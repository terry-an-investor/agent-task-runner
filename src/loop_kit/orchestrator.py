"""
PM-driven review loop orchestrator.

Usage:
    loop init                                    # create .loop/ dirs
    loop run --task .loop/task_card.json         # full loop
    loop status                                  # show current state
    loop archive --task-id T-604                 # list archived bus files
    loop extract-diff BASE HEAD                  # manual diff extraction

Protocol (file bus in .loop/):
    PM  -> Worker:   task_card.json  / fix_list.json
    Worker -> PM:    work_report.json
    PM  -> Reviewer: review_request.json
    Reviewer -> PM:  review_report.json

All messages are JSON. Git commits are the single source of truth.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path.cwd()
LOOP_DIR = ROOT / ".loop"
LOGS_DIR = LOOP_DIR / "logs"
RUNTIME_DIR = LOOP_DIR / "runtime"
ARCHIVE_DIR = LOOP_DIR / "archive"
STATE_FILE = LOOP_DIR / "state.json"

DEFAULT_MAX_ROUNDS = 3
POLL_INTERVAL_SEC = 5
DEFAULT_HEARTBEAT_TTL_SEC = 30
DEFAULT_DISPATCH_TIMEOUT_SEC = 600
DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC = 90
DEFAULT_DISPATCH_BACKEND = "native"
DISPATCH_STREAM_POLL_SEC = 0.1
_FEED_TASK_ID: str | None = None


def hello() -> str:
    return "hello from loop-kit"


class DispatchTimeoutError(RuntimeError):
    """Dispatch command timed out before process exit."""


# ── file paths ──────────────────────────────────────────────────────
def _path(name: str) -> Path:
    return LOOP_DIR / name

TASK_CARD = _path("task_card.json")
FIX_LIST = _path("fix_list.json")
WORK_REPORT = _path("work_report.json")
REVIEW_REQ = _path("review_request.json")
REVIEW_REPORT = _path("review_report.json")
LOCK_FILE = _path("lock")


def _resolve_loop_dir(loop_dir: str | Path) -> Path:
    candidate = Path(loop_dir)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def _configure_loop_paths(loop_dir: str | Path = ".loop") -> None:
    global LOOP_DIR
    global LOGS_DIR
    global RUNTIME_DIR
    global ARCHIVE_DIR
    global STATE_FILE
    global TASK_CARD
    global FIX_LIST
    global WORK_REPORT
    global REVIEW_REQ
    global REVIEW_REPORT
    global LOCK_FILE

    LOOP_DIR = _resolve_loop_dir(loop_dir)
    LOGS_DIR = LOOP_DIR / "logs"
    RUNTIME_DIR = LOOP_DIR / "runtime"
    ARCHIVE_DIR = LOOP_DIR / "archive"
    STATE_FILE = LOOP_DIR / "state.json"
    TASK_CARD = _path("task_card.json")
    FIX_LIST = _path("fix_list.json")
    WORK_REPORT = _path("work_report.json")
    REVIEW_REQ = _path("review_request.json")
    REVIEW_REPORT = _path("review_report.json")
    LOCK_FILE = _path("lock")


def _loop_templates_dir() -> Path:
    return LOOP_DIR / "templates"


def _worker_prompt_template_path() -> Path:
    return _loop_templates_dir() / "worker_prompt.txt"


def _reviewer_prompt_template_path() -> Path:
    return _loop_templates_dir() / "reviewer_prompt.txt"


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _task_archive_dir(task_id: str) -> Path:
    return ARCHIVE_DIR / task_id


def _archive_bus_file(path: Path, task_id: str, round_num: int, suffix: str) -> Path | None:
    if not path.exists():
        return None
    archive_dir = _task_archive_dir(task_id)
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"r{round_num}_{suffix}.json"
    shutil.copy2(path, dest)
    return dest


def _archive_task_summary(task_id: str) -> Path | None:
    summary_path = LOOP_DIR / "summary.json"
    if not summary_path.exists():
        return None
    archive_dir = _task_archive_dir(task_id)
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / "summary.json"
    shutil.copy2(summary_path, dest)
    return dest


def _archive_state_for_round(task_id: str, round_num: int) -> Path | None:
    """Capture the pre-round state snapshot once for this round."""
    dest = _task_archive_dir(task_id) / f"r{round_num}_state.json"
    if dest.exists():
        return dest
    return _archive_bus_file(STATE_FILE, task_id, round_num, "state")


class _LoopLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            handle.close()
            raise RuntimeError(f"another orchestrator instance is already running ({self.path})") from e
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "_LoopLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _acquire_run_lock() -> _LoopLock:
    lock = _LoopLock(LOCK_FILE)
    lock.acquire()
    return lock

def _heartbeat_path(role: str) -> Path:
    return RUNTIME_DIR / f"{role}.heartbeat.json"

def _dispatch_log_path(role: str) -> Path:
    return LOGS_DIR / f"{role}_dispatch.log"

def _feed_log_path() -> Path:
    return LOGS_DIR / "feed.jsonl"

# ── logging ─────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _set_feed_task_id(task_id: str | None) -> None:
    global _FEED_TASK_ID
    _FEED_TASK_ID = task_id

def _feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
    payload_data = dict(data or {})
    if _FEED_TASK_ID and payload_data.get("task_id") not in (None, _FEED_TASK_ID):
        return
    if _FEED_TASK_ID and "task_id" not in payload_data:
        payload_data["task_id"] = _FEED_TASK_ID
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": _ts(),
        "level": level,
        "event": event,
        "data": payload_data,
    }
    with open(_feed_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

def _log(msg: str) -> None:
    ts = _ts()
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOGS_DIR / "orchestrator.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    _feed_event("log", data={"message": msg})


def _normalized_abs(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))

def _read_json_if_exists(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

def _heartbeat_age_sec(path: Path, now: float | None = None) -> float | None:
    if not path.exists():
        return None
    if now is None:
        now = time.time()
    return max(0.0, now - path.stat().st_mtime)

def _role_is_alive(role: str, ttl_sec: int) -> tuple[bool, str]:
    hb = _heartbeat_path(role)
    age = _heartbeat_age_sec(hb)
    if age is None:
        return False, f"{role} heartbeat missing ({hb})"
    if age > ttl_sec:
        return False, f"{role} heartbeat stale: age={age:.1f}s > ttl={ttl_sec}s ({hb})"
    data = _read_json_if_exists(hb)
    pid = data.get("pid") if isinstance(data, dict) else "?"
    return True, f"{role} alive (pid={pid}, age={age:.1f}s)"

def _extract_codex_thread_id(stdout: str) -> str | None:
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "thread.started":
            tid = obj.get("thread_id")
            if isinstance(tid, str) and tid:
                return tid
    return None


def _flatten_text_payload(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_flatten_text_payload(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "message", "content", "output_text", "value"):
            if key in value:
                text = _flatten_text_payload(value.get(key))
                if text:
                    return text
    return ""


def _truncate_summary_text(text: str, max_len: int = 120) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3].rstrip() + "..."


def _extract_command_summary(item: dict) -> str:
    command = item.get("command")
    if isinstance(command, str) and command.strip():
        return _truncate_summary_text(command)
    if isinstance(command, list):
        rendered = " ".join(str(part) for part in command if isinstance(part, (str, int, float)))
        if rendered.strip():
            return _truncate_summary_text(rendered)
    call = item.get("call")
    if isinstance(call, dict):
        return _extract_command_summary(call)
    return ""


def _codex_event_summary(role: str, backend: str, line: str) -> str | None:
    if backend != "codex":
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "item.completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type == "command_execution":
        command = _extract_command_summary(item)
        return f"[{role}] Running: {command}" if command else f"[{role}] Running command"
    if item_type == "agent_message":
        message = _flatten_text_payload(item)
        return f"[{role}] Message: {_truncate_summary_text(message)}" if message else f"[{role}] Message"
    return None


def _stream_dispatch_stdout_line(role: str, backend: str, raw_line: str, *, verbose: bool) -> None:
    line = raw_line.rstrip("\r\n")
    if verbose:
        print(line, flush=True)
        return
    summary = _codex_event_summary(role, backend, line)
    if summary:
        print(summary, flush=True)


BackendBuildFn = Callable[[str, str], tuple[list[str], str | None, str | None]]
BackendResolveFn = Callable[[str], str]
_BACKEND_REGISTRY: dict[str, tuple[BackendBuildFn, BackendResolveFn]] = {}


def _available_backends() -> list[str]:
    return sorted(_BACKEND_REGISTRY.keys())


def register_backend(name: str, build_cmd_fn: BackendBuildFn, resolve_exe_fn: BackendResolveFn) -> None:
    backend = name.strip().lower()
    if not backend:
        raise ValueError("backend name must not be empty")
    _BACKEND_REGISTRY[backend] = (build_cmd_fn, resolve_exe_fn)


def _require_registered_backend(backend: str) -> tuple[BackendBuildFn, BackendResolveFn]:
    key = backend.strip().lower()
    spec = _BACKEND_REGISTRY.get(key)
    if spec is None:
        raise ValueError(
            f"Unsupported backend: {backend}. "
            f"Registered backends: {', '.join(_available_backends()) or '<none>'}"
        )
    return spec


def _resolve_exe_from_candidates(*, backend: str, candidates: list[str | None]) -> str:
    for exe in candidates:
        if exe and Path(exe).exists():
            return exe
    raise RuntimeError(f"Cannot find executable for backend={backend}")


def _resolve_codex_exe(backend: str) -> str:
    return _resolve_exe_from_candidates(
        backend=backend,
        candidates=[
            shutil.which("codex"),
            shutil.which("codex.cmd"),
            str(Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"),
            str(Path.home() / "AppData" / "Roaming" / "npm" / "codex"),
        ],
    )


def _resolve_claude_exe(backend: str) -> str:
    return _resolve_exe_from_candidates(
        backend=backend,
        candidates=[
            shutil.which("claude"),
            shutil.which("claude.exe"),
            str(Path.home() / ".local" / "bin" / "claude.exe"),
        ],
    )


def _build_codex_command(exe: str, prompt: str) -> tuple[list[str], str | None, str | None]:
    return ([
        exe, "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C", str(ROOT),
        "Execute the context provided via stdin.  Follow the instructions embedded in it and only finish after the required output artifact is written.",
    ], None, prompt)


def _build_claude_command(exe: str, prompt: str) -> tuple[list[str], str | None, str | None]:
    sid = str(uuid.uuid4())
    return ([
        exe,
        "-p",
        "--dangerously-skip-permissions",
        "--session-id", sid,
        prompt,
    ], sid, None)


def _resolve_backend_exe(backend: str) -> str:
    _, resolve_exe_fn = _require_registered_backend(backend)
    return resolve_exe_fn(backend.strip().lower())


def _resolve_par_exe(par_bin: str) -> str:
    if os.path.sep in par_bin or (os.path.altsep and os.path.altsep in par_bin):
        if Path(par_bin).exists():
            return par_bin
        raise RuntimeError(f"Cannot find par executable: {par_bin}")
    found = shutil.which(par_bin)
    if found:
        return found
    raise RuntimeError(
        f"Cannot find par executable '{par_bin}'. Install par or pass --par-bin <path>."
    )

def _agent_command(backend: str, prompt: str) -> tuple[list[str], str | None, str | None]:
    """Return (cmd, session_id, stdin_text).

    For codex >= 0.118.0 the prompt context is piped via stdin so the
    command line stays short.  The short CLI arg is a one-line instruction.
    """
    build_cmd_fn, _ = _require_registered_backend(backend)
    exe = _resolve_backend_exe(backend)
    return build_cmd_fn(exe, prompt)


register_backend("codex", _build_codex_command, _resolve_codex_exe)
register_backend("claude", _build_claude_command, _resolve_claude_exe)


def _write_dispatch_log(
    role: str,
    cmd: list[str],
    result: subprocess.CompletedProcess[str],
    session_id: str | None,
) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log = _dispatch_log_path(role)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"[{_ts()}] role={role} returncode={result.returncode}\n")
        if session_id:
            f.write(f"session_id={session_id}\n")
        f.write(f"cmd={' '.join(cmd)}\n")
        if result.stdout:
            f.write("stdout:\n")
            f.write(result.stdout)
            if not result.stdout.endswith("\n"):
                f.write("\n")
        if result.stderr:
            f.write("stderr:\n")
            f.write(result.stderr)
            if not result.stderr.endswith("\n"):
                f.write("\n")
        f.write("-" * 60 + "\n")

def _dispatch_failure_hint(*, backend: str, stderr: str, timeout: bool = False) -> str:
    hints: list[str] = []
    lowered = stderr.lower()
    if timeout:
        hints.append("try --dispatch-timeout 900.")
    if any(token in lowered for token in ("auth", "unauthorized", "401", "api key", "token")):
        if backend == "codex":
            hints.append("check codex API key/login.")
        elif backend == "claude":
            hints.append("check claude authentication/session.")
        else:
            hints.append("check backend authentication.")
    if any(token in lowered for token in ("not found", "no such file", "cannot find")):
        hints.append("verify backend executable path and installation.")
    if not hints:
        hints.append("check backend auth/network and retry.")
    return " Remediation: " + " ".join(hints)


def _collect_streamed_process_output(
    proc: subprocess.Popen[str],
    *,
    role: str,
    backend: str,
    stdin_text: str | None,
    timeout_sec: int,
    verbose: bool,
) -> tuple[str, str, int, bool]:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdin_thread: threading.Thread | None = None

    def _close_pipe(pipe) -> None:
        if pipe is None:
            return
        close = getattr(pipe, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass

    def _write_stdin() -> None:
        if stdin_text is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(stdin_text)
        except OSError:
            pass
        finally:
            _close_pipe(proc.stdin)

    def _read_pipe(pipe, sink: list[str], line_callback=None) -> None:
        if pipe is None:
            return
        try:
            for raw_line in pipe:
                sink.append(raw_line)
                if line_callback is not None:
                    line_callback(raw_line)
        finally:
            _close_pipe(pipe)

    if stdin_text is not None and proc.stdin is not None:
        stdin_thread = threading.Thread(target=_write_stdin, daemon=True)
        stdin_thread.start()

    stdout_thread = threading.Thread(
        target=_read_pipe,
        args=(
            proc.stdout,
            stdout_chunks,
            lambda raw_line: _stream_dispatch_stdout_line(
                role,
                backend,
                raw_line,
                verbose=verbose,
            ),
        ),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_pipe,
        args=(proc.stderr, stderr_chunks, None),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    deadline = None if timeout_sec <= 0 else (time.monotonic() + timeout_sec)
    timed_out = False
    while proc.poll() is None:
        if deadline is not None and time.monotonic() > deadline:
            timed_out = True
            _close_pipe(proc.stdin)
            proc.kill()
            break
        time.sleep(DISPATCH_STREAM_POLL_SEC)

    returncode = proc.wait()
    stdout_thread.join()
    stderr_thread.join()
    if stdin_thread is not None:
        stdin_thread.join(timeout=DISPATCH_STREAM_POLL_SEC)
    return "".join(stdout_chunks), "".join(stderr_chunks), returncode, timed_out


def _collect_streamed_text_output(
    proc: subprocess.Popen[str],
    *,
    stdout_line_callback: Callable[[str], None] | None = None,
) -> tuple[str, str, int]:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _close_pipe(pipe) -> None:
        if pipe is None:
            return
        close = getattr(pipe, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass

    def _read_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            for raw_line in proc.stderr:
                stderr_chunks.append(raw_line)
        finally:
            _close_pipe(proc.stderr)

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    if proc.stdout is not None:
        try:
            for raw_line in proc.stdout:
                stdout_chunks.append(raw_line)
                if stdout_line_callback is not None:
                    stdout_line_callback(raw_line)
        finally:
            _close_pipe(proc.stdout)

    returncode = proc.wait()
    stderr_thread.join()
    return "".join(stdout_chunks), "".join(stderr_chunks), returncode


def _run_auto_dispatch(
    role: str,
    backend: str,
    prompt: str,
    timeout_sec: int,
    *,
    verbose: bool = False,
) -> None:
    _log(f"Auto-dispatch start: role={role} backend={backend}")
    cmd, cmd_sid, stdin_text = _agent_command(backend, prompt)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdin=(subprocess.PIPE if stdin_text is not None else None),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    stdout, stderr, returncode, timed_out = _collect_streamed_process_output(
        proc,
        role=role,
        backend=backend,
        stdin_text=stdin_text,
        timeout_sec=timeout_sec,
        verbose=verbose,
    )
    if timed_out:
        result = subprocess.CompletedProcess(
            cmd,
            returncode if returncode is not None else -9,
            stdout,
            stderr,
        )
        _write_dispatch_log(role, cmd, result, cmd_sid)
        _feed_event(
            "dispatch_summary",
            level="error",
            data={
                "role": role,
                "mode": "native",
                "backend": backend,
                "timeout_sec": timeout_sec,
                "returncode": result.returncode,
            },
        )
        raise DispatchTimeoutError(
            f"{role} dispatch timeout after {timeout_sec}s (backend={backend})."
            + _dispatch_failure_hint(backend=backend, stderr=stderr or "", timeout=True)
        )
    result = subprocess.CompletedProcess(
        cmd,
        returncode if returncode is not None else 1,
        stdout,
        stderr,
    )

    session_id = cmd_sid
    if backend == "codex":
        parsed = _extract_codex_thread_id(result.stdout or "")
        if parsed:
            session_id = parsed
    _write_dispatch_log(role, cmd, result, session_id)
    _feed_event(
        "dispatch_summary",
        level=("info" if result.returncode == 0 else "error"),
        data={
            "role": role,
            "mode": "native",
            "backend": backend,
            "returncode": result.returncode,
            "session_id": session_id,
            "stdout_len": len(result.stdout or ""),
            "stderr_len": len(result.stderr or ""),
        },
    )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(
            f"{role} dispatch failed (backend={backend}, rc={result.returncode}): {stderr}"
            + _dispatch_failure_hint(backend=backend, stderr=stderr)
        )
    _log(f"Auto-dispatch done: role={role} backend={backend}")

def _run_par_dispatch(
    role: str,
    target: str,
    prompt: str,
    timeout_sec: int,
    par_bin: str,
) -> None:
    exe = _resolve_par_exe(par_bin)
    cmd = [exe, "send", target, prompt]
    _log(f"Par-dispatch start: role={role} target={target} par={exe}")
    timeout = None if timeout_sec <= 0 else timeout_sec
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        _feed_event(
            "dispatch_summary",
            level="error",
            data={"role": role, "mode": "par", "target": target, "timeout_sec": timeout_sec},
        )
        raise DispatchTimeoutError(
            f"{role} par-dispatch timeout after {timeout_sec}s (target={target}): {e}."
            + _dispatch_failure_hint(backend="par", stderr="", timeout=True)
        ) from e
    _write_dispatch_log(role, cmd, result, None)
    _feed_event(
        "dispatch_summary",
        level=("info" if result.returncode == 0 else "error"),
        data={
            "role": role,
            "mode": "par",
            "target": target,
            "returncode": result.returncode,
            "stdout_len": len(result.stdout or ""),
            "stderr_len": len(result.stderr or ""),
        },
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(
            f"{role} par-dispatch failed (target={target}, rc={result.returncode}): {stderr}"
            + _dispatch_failure_hint(backend="par", stderr=stderr)
        )
    _log(f"Par-dispatch done: role={role} target={target}")

def _require_dispatch_artifact(
    role: str,
    path: Path,
    task_id: str,
    round_num: int,
    timeout_sec: int = DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
) -> dict:
    data = _wait_for_file(
        path=path,
        description=f"{role} post-dispatch artifact check",
        timeout_sec=timeout_sec,
        expected_task_id=task_id,
        expected_round=round_num,
        show_manual_hint=False,
    )
    if data is None:
        raise RuntimeError(
            f"{role} dispatch returned success but {path.name} was not produced "
            f"for task_id={task_id} round={round_num} within {timeout_sec}s"
        )
    return data


def _dispatch_with_artifact_fallback(
    *,
    role: str,
    dispatch_call,
    artifact_path: Path,
    task_id: str,
    round_num: int,
    timeout_sec: int = DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
) -> dict:
    try:
        dispatch_call()
    except DispatchTimeoutError as e:
        _log(
            f"{role} dispatch timed out; checking {artifact_path.name} "
            f"for task_id={task_id} round={round_num}"
        )
        data = _wait_for_file(
            path=artifact_path,
            description=f"{role} post-timeout artifact check",
            timeout_sec=timeout_sec,
            expected_task_id=task_id,
            expected_round=round_num,
            show_manual_hint=False,
        )
        if data is not None:
            _log(f"{role} dispatch timed out but {artifact_path.name} is present; continuing")
            return data
        raise RuntimeError(str(e)) from e
    return _require_dispatch_artifact(
        role=role,
        path=artifact_path,
        task_id=task_id,
        round_num=round_num,
        timeout_sec=timeout_sec,
    )

def _read_text_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _as_prompt_list(items: object) -> str:
    if not isinstance(items, list) or not items:
        return "- <none>"
    return "\n".join(f"- {item}" for item in items)


def _render_task_card_section(task_card: dict) -> str:
    return (
        "=== TASK CARD ===\n"
        f"goal: {task_card.get('goal', '<none>')}\n"
        "in_scope:\n"
        f"{_as_prompt_list(task_card.get('in_scope'))}\n"
        "out_of_scope:\n"
        f"{_as_prompt_list(task_card.get('out_of_scope'))}\n"
        "acceptance_criteria:\n"
        f"{_as_prompt_list(task_card.get('acceptance_criteria'))}\n"
        "constraints:\n"
        f"{_as_prompt_list(task_card.get('constraints'))}\n"
    )


def _render_prior_round_context_section(round_num: int) -> str | None:
    if round_num <= 1:
        return None
    work = _read_json_if_exists(WORK_REPORT)
    review = _read_json_if_exists(REVIEW_REPORT)
    if not isinstance(work, dict) or not isinstance(review, dict):
        return None

    blocking = review.get("blocking_issues", [])
    if isinstance(blocking, list) and blocking:
        blocking_summary = "\n".join(
            f"- [{issue.get('severity', '?')}] {issue.get('file', '')}: {issue.get('reason', '')}"
            for issue in blocking
            if isinstance(issue, dict)
        )
        if not blocking_summary:
            blocking_summary = "- <none>"
    else:
        blocking_summary = "- <none>"

    return (
        "=== PRIOR ROUND CONTEXT ===\n"
        f"prior_round_notes: {work.get('notes', '')}\n"
        "prior_round_files_changed:\n"
        f"{_as_prompt_list(work.get('files_changed'))}\n"
        "prior_review_blocking_issues:\n"
        f"{blocking_summary}\n"
        "prior_review_non_blocking:\n"
        f"{_as_prompt_list(review.get('non_blocking_suggestions'))}\n"
    )


DEFAULT_WORKER_PROMPT_TEMPLATE = (
    "Role: code-writer worker for PM loop.\n"
    "Current task_id: {task_id}, round: {round_num}.\n"
    "Execute the contract below and only finish after writing {work_report_path}.\n\n"
    "=== BEGIN AGENTS.md ===\n"
    "{agents_md}\n"
    "=== END AGENTS.md ===\n\n"
    "=== BEGIN docs/roles/code-writer.md ===\n"
    "{role_md}\n"
    "=== END docs/roles/code-writer.md ===\n\n"
    "{task_card_section}{prior_context_section}"
)

DEFAULT_REVIEWER_PROMPT_TEMPLATE = (
    "Role: reviewer for PM loop.\n"
    "Current task_id: {task_id}, round: {round_num}.\n"
    "Execute the contract below and only finish after writing {review_report_path}.\n\n"
    "=== BEGIN docs/roles/reviewer.md ===\n"
    "{role_md}\n"
    "=== END docs/roles/reviewer.md ===\n"
)


def _render_prompt_template(
    *,
    template_path: Path,
    context: dict[str, str],
) -> str:
    template_text = _read_text_optional(template_path)
    if template_text is None:
        raise RuntimeError(
            f"Missing required prompt template: {_display_path(template_path)}. "
            "Run loop init to create required files "
            "(loop init)."
        )
    try:
        return template_text.format(**context)
    except (KeyError, ValueError) as e:
        raise RuntimeError(f"Invalid prompt template at {_display_path(template_path)}: {e}") from e


def _read_required_text(path: Path, *, label: str) -> str:
    text = _read_text_optional(path)
    if text:
        return text
    raise RuntimeError(
        f"Missing required {label}: {_display_path(path)}. "
        "Run loop init to create required files "
        "(loop init)."
    )


def _worker_prompt(task_id: str, round_num: int) -> str:
    agents_text = _read_required_text(ROOT / "AGENTS.md", label="AGENTS.md")
    role_text = _read_required_text(ROOT / "docs" / "roles" / "code-writer.md", label="code-writer role doc")
    task_card = _read_json_if_exists(TASK_CARD)
    task_card_section = _render_task_card_section(task_card if isinstance(task_card, dict) else {})
    prior_context_section = _render_prior_round_context_section(round_num)
    context = {
        "task_id": task_id,
        "round_num": str(round_num),
        "agents_md": agents_text,
        "role_md": role_text,
        "task_card_section": task_card_section,
        "prior_context_section": (f"\n{prior_context_section}" if prior_context_section else ""),
        "work_report_path": _display_path(WORK_REPORT),
    }
    return _render_prompt_template(
        template_path=_worker_prompt_template_path(),
        context=context,
    )


def _reviewer_prompt(task_id: str, round_num: int) -> str:
    role_text = _read_required_text(ROOT / "docs" / "roles" / "reviewer.md", label="reviewer role doc")
    context = {
        "task_id": task_id,
        "round_num": str(round_num),
        "agents_md": "",
        "role_md": role_text,
        "task_card_section": "",
        "prior_context_section": "",
        "review_report_path": _display_path(REVIEW_REPORT),
    }
    return _render_prompt_template(
        template_path=_reviewer_prompt_template_path(),
        context=context,
    )

# ── state ───────────────────────────────────────────────────────────
STATE_IDLE            = "idle"
STATE_AWAITING_WORK   = "awaiting_work"
STATE_AWAITING_REVIEW = "awaiting_review"
STATE_DONE            = "done"

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"state": STATE_IDLE, "round": 0, "task_id": None}

def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")

# ── git helpers ─────────────────────────────────────────────────────
def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT)] + list(args),
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()

def _current_sha() -> str:
    return _git("rev-parse", "HEAD")

def _diff(base: str, head: str) -> str:
    return _git("diff", f"{base}..{head}")

def _log_oneline(base: str, head: str) -> str:
    return _git("log", "--oneline", f"{base}..{head}")

def _is_git_repo_root(path: Path) -> bool:
    return (path / ".git").exists()

def _parse_porcelain_path(raw: str) -> str:
    text = raw.strip()
    if " -> " in text:
        text = text.split(" -> ", 1)[1]
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        text = text[1:-1]
    return text.replace("\\", "/")

def _dirty_tracked_paths() -> list[str]:
    if not _is_git_repo_root(ROOT):
        return []
    status = _git("status", "--porcelain")
    dirty: list[str] = []
    for raw in status.splitlines():
        if len(raw) < 3:
            continue
        xy = raw[:2]
        if xy == "??":
            # Known local scratch files are usually untracked; ignore them.
            continue
        path = _parse_porcelain_path(raw[3:])
        if not path or path.startswith(".loop/"):
            continue
        dirty.append(path)
    return sorted(set(dirty))

def _enforce_clean_worktree_or_exit(*, allow_dirty: bool) -> None:
    dirty = _dirty_tracked_paths()
    if not dirty:
        return
    _log(f"Dirty working tree detected ({len(dirty)} tracked files)")
    print("Warning: dirty git working tree detected:", file=sys.stderr)
    for path in dirty:
        print(f"  - {path}", file=sys.stderr)
    if allow_dirty:
        print("Proceeding because --allow-dirty is set.", file=sys.stderr)
        return
    print("Refusing to start. Re-run with --allow-dirty to bypass.", file=sys.stderr)
    sys.exit(4)

def _validate_work_report(
    work: dict,
    *,
    expected_task_id: str,
    expected_round: int,
) -> str | None:
    required_types: dict[str, type] = {
        "task_id": str,
        "head_sha": str,
        "round": int,
    }
    for field, typ in required_types.items():
        if field not in work:
            return f"work_report.json missing required field '{field}'"
        value = work[field]
        if typ is int:
            if type(value) is not int:
                return f"work_report field '{field}' must be int, got {type(value).__name__}"
        elif not isinstance(value, typ):
            return f"work_report field '{field}' must be {typ.__name__}, got {type(value).__name__}"
        if typ is str and not value.strip():
            return f"work_report field '{field}' must be non-empty"

    if work["task_id"] != expected_task_id:
        return (
            "work_report field 'task_id' mismatch: "
            f"expected {expected_task_id!r}, got {work['task_id']!r}"
        )
    if work["round"] != expected_round:
        return (
            "work_report field 'round' mismatch: "
            f"expected {expected_round}, got {work['round']!r}"
        )
    return None

def _tests_summary(tests: object) -> dict:
    if not isinstance(tests, list):
        return {"total": 0, "pass": 0, "fail": 0, "other": 0}
    summary = {"total": len(tests), "pass": 0, "fail": 0, "other": 0}
    for item in tests:
        result = item.get("result") if isinstance(item, dict) else None
        if result == "pass":
            summary["pass"] += 1
        elif result in {"fail", "failed", "error"}:
            summary["fail"] += 1
        else:
            summary["other"] += 1
    return summary

# ── polling ─────────────────────────────────────────────────────────
def _wait_for_file(
    path: Path,
    description: str,
    timeout_sec: int = 0,
    expected_task_id: str | None = None,
    expected_round: int | None = None,
    expected_role: str | None = None,
    heartbeat_ttl_sec: int = DEFAULT_HEARTBEAT_TTL_SEC,
    show_manual_hint: bool = True,
) -> dict | None:
    """Poll until *path* appears. Returns parsed JSON or None on timeout."""
    _log(f"Waiting for {path.name} ({description}) ...")
    if show_manual_hint:
        print(f"\n  >>> Tell the {'Worker' if 'work' in path.name else 'Reviewer'} "
              f"to process their input file. <<<\n")
    elapsed = 0
    last_ignored_signature: tuple[int, int] | None = None
    while True:
        if expected_role is not None:
            alive, reason = _role_is_alive(expected_role, heartbeat_ttl_sec)
            if not alive:
                _log(f"Stopping wait: {reason}")
                return None
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                _log(f"{path.name} has invalid JSON: {e}")
            else:
                task_id = data.get("task_id")
                report_has_round = "round" in data
                round_num = data.get("round")
                if expected_task_id is not None and task_id != expected_task_id:
                    stat = path.stat()
                    last_ignored_signature = (stat.st_mtime_ns, stat.st_size)
                elif expected_round is not None and report_has_round and round_num != expected_round:
                    stat = path.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    if signature != last_ignored_signature:
                        _log(f"Ignoring stale {path.name}: expected round={expected_round}, got {round_num!r}")
                        last_ignored_signature = signature
                else:
                    _log(f"Found {path.name}")
                    return data
        if timeout_sec and elapsed >= timeout_sec:
            _log(f"Timeout ({timeout_sec}s) waiting for {path.name}")
            return None
        time.sleep(POLL_INTERVAL_SEC)
        elapsed += POLL_INTERVAL_SEC


def _fail_with_state(state: dict, outcome: str, message: str, exit_code: int = 1) -> None:
    _log(message)
    print(f"  Error: {message}", file=sys.stderr)
    state["state"] = STATE_DONE
    state["outcome"] = outcome
    state["failed_at"] = _ts()
    state["error"] = message
    _save_state(state)
    sys.exit(exit_code)


def _write_template_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


# ── init ────────────────────────────────────────────────────────────
def cmd_init() -> None:
    LOOP_DIR.mkdir(exist_ok=True)
    (LOOP_DIR / "examples").mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    RUNTIME_DIR.mkdir(exist_ok=True)
    ARCHIVE_DIR.mkdir(exist_ok=True)
    templates_dir = _loop_templates_dir()
    templates_dir.mkdir(exist_ok=True)
    _log(f"Initialized loop directory: {LOOP_DIR}")
    print(f"  Created: {LOOP_DIR}")
    print(f"  Created: {LOGS_DIR}")
    print(f"  Created: {RUNTIME_DIR}")
    print(f"  Created: {ARCHIVE_DIR}")
    print(f"  Created: {templates_dir}")
    # copy example task card if not present
    example = LOOP_DIR / "examples" / "task_card.json"
    if not example.exists():
        example.write_text(json.dumps({
            "task_id": "T-001",
            "goal": "<one-sentence goal>",
            "in_scope": ["<file or module>"],
            "out_of_scope": [],
            "acceptance_criteria": ["<measurable criterion>"],
            "constraints": []
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  Created: {example}")
    worker_template = _worker_prompt_template_path()
    if _write_template_if_missing(worker_template, DEFAULT_WORKER_PROMPT_TEMPLATE + "\n"):
        print(f"  Created: {worker_template}")
    reviewer_template = _reviewer_prompt_template_path()
    if _write_template_if_missing(reviewer_template, DEFAULT_REVIEWER_PROMPT_TEMPLATE + "\n"):
        print(f"  Created: {reviewer_template}")

# ── status ──────────────────────────────────────────────────────────
def cmd_status() -> None:
    state = _load_state()
    print(json.dumps(state, indent=2, ensure_ascii=False))
    # also show which files exist
    for p in [TASK_CARD, WORK_REPORT, REVIEW_REQ, REVIEW_REPORT, FIX_LIST]:
        marker = "EXISTS" if p.exists() else "missing"
        print(f"  {p.name}: {marker}")
    for role in ("worker", "reviewer"):
        hb = _heartbeat_path(role)
        marker = "EXISTS" if hb.exists() else "missing"
        print(f"  {hb.name}: {marker}")


def _restore_target_name_from_archive(stem: str) -> str:
    if stem == "summary":
        return "summary.json"
    prefix, sep, suffix = stem.partition("_")
    if sep and prefix.startswith("r") and prefix[1:].isdigit() and suffix:
        return f"{suffix}.json"
    return f"{stem}.json"


def cmd_archive(task_id: str, restore: str | None = None) -> None:
    archive_dir = _task_archive_dir(task_id)
    if restore is None:
        if not archive_dir.exists():
            print(f"No archive directory for task_id={task_id}: {archive_dir}")
            return
        files = sorted(path.name for path in archive_dir.glob("*.json") if path.is_file())
        if not files:
            print(f"No archived files for task_id={task_id}: {archive_dir}")
            return
        print(f"Archive directory: {archive_dir}")
        for name in files:
            print(f"  {name}")
        return

    restore_name = restore if restore.endswith(".json") else f"{restore}.json"
    src = archive_dir / restore_name
    if not src.exists():
        print(
            f"Error: archive file not found for task_id={task_id}: {src}",
            file=sys.stderr,
        )
        sys.exit(1)
    target_name = _restore_target_name_from_archive(src.stem)
    dest = LOOP_DIR / target_name
    shutil.copy2(src, dest)
    print(f"Restored {src.name} -> {dest}")

# ── extract-diff ────────────────────────────────────────────────────
def cmd_extract_diff(base: str, head: str) -> None:
    print(_diff(base, head))

def cmd_heartbeat(role: str, interval: int) -> None:
    role = role.lower().strip()
    if role not in {"worker", "reviewer"}:
        print(f"Error: invalid role: {role}", file=sys.stderr)
        sys.exit(1)
    LOOP_DIR.mkdir(exist_ok=True)
    RUNTIME_DIR.mkdir(exist_ok=True)
    hb = _heartbeat_path(role)
    _log(f"Heartbeat started for role={role} interval={interval}s")
    print(f"  Writing heartbeat: {hb}")
    print("  Press Ctrl+C to stop.")
    try:
        while True:
            payload = {
                "role": role,
                "pid": os.getpid(),
                "updated_at": _ts(),
                "cwd": str(ROOT),
            }
            hb.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
            time.sleep(max(1, interval))
    except KeyboardInterrupt:
        _log(f"Heartbeat stopped for role={role}")
        print("\n  Heartbeat stopped.")
        sys.exit(0)

def cmd_health(ttl: int) -> None:
    for role in ("worker", "reviewer"):
        alive, reason = _role_is_alive(role, ttl)
        status = "alive" if alive else "dead"
        print(f"  {role}: {status}  ({reason})")

# ── main run loop ───────────────────────────────────────────────────
def _load_task_card(task_path: str) -> tuple[Path, dict, str]:
    tp = Path(task_path)
    if not tp.exists():
        print(f"Error: task card not found: {tp}", file=sys.stderr)
        sys.exit(1)
    task_card = json.loads(tp.read_text(encoding="utf-8"))
    task_id = task_card.get("task_id", "UNKNOWN")
    return tp, task_card, task_id


def _sync_task_card_to_bus(task_path: str, round_num: int = 1) -> tuple[dict, str]:
    tp, task_card, task_id = _load_task_card(task_path)
    if _normalized_abs(tp) != _normalized_abs(TASK_CARD):
        _archive_bus_file(TASK_CARD, task_id, round_num, "task_card")
        shutil.copy2(tp, TASK_CARD)
    return task_card, task_id


def _single_round_subprocess_cmd(
    *,
    round_num: int,
    timeout: int,
    require_heartbeat: bool,
    heartbeat_ttl: int,
    auto_dispatch: bool,
    dispatch_backend: str,
    worker_backend: str,
    reviewer_backend: str,
    dispatch_timeout: int,
    artifact_timeout: int,
    par_bin: str,
    par_worker_target: str,
    par_reviewer_target: str,
    allow_dirty: bool,
    verbose: bool,
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "loop_kit",
        "run",
        "--single-round",
        "--round",
        str(round_num),
        "--loop-dir",
        _display_path(LOOP_DIR),
        "--task",
        str(TASK_CARD),
        "--timeout",
        str(timeout),
        "--heartbeat-ttl",
        str(heartbeat_ttl),
        "--dispatch-backend",
        dispatch_backend,
        "--worker-backend",
        worker_backend,
        "--reviewer-backend",
        reviewer_backend,
        "--dispatch-timeout",
        str(dispatch_timeout),
        "--artifact-timeout",
        str(artifact_timeout),
        "--par-bin",
        par_bin,
        "--par-worker-target",
        par_worker_target,
        "--par-reviewer-target",
        par_reviewer_target,
    ]
    if require_heartbeat:
        cmd.append("--require-heartbeat")
    if auto_dispatch:
        cmd.append("--auto-dispatch")
    if allow_dirty:
        cmd.append("--allow-dirty")
    if verbose:
        cmd.append("--verbose")
    return cmd


def _run_single_round(
    *,
    task_path: str,
    round_num: int,
    timeout: int,
    require_heartbeat: bool,
    heartbeat_ttl: int,
    auto_dispatch: bool,
    dispatch_backend: str,
    worker_backend: str,
    reviewer_backend: str,
    dispatch_timeout: int,
    artifact_timeout: int,
    par_bin: str,
    par_worker_target: str,
    par_reviewer_target: str,
    verbose: bool,
) -> None:
    task_card, task_id_from_card = _sync_task_card_to_bus(task_path, round_num=round_num)

    state = _load_state()
    state_task_id = state.get("task_id")
    state_base_sha = state.get("base_sha")

    def _save_single_round_state() -> None:
        _archive_state_for_round(task_id_from_card, round_num)
        _save_state(state)

    def _fail_single_round(outcome: str, message: str, exit_code: int = 3) -> None:
        _archive_state_for_round(task_id_from_card, round_num)
        _fail_with_state(
            state,
            outcome=outcome,
            message=message,
            exit_code=exit_code,
        )

    if not state_task_id or not state_base_sha:
        if round_num != 1:
            _fail_single_round(
                outcome="state_contract_missing",
                message=(
                    "single-round requires existing state contract for round>1: "
                    f"task_id={state_task_id!r} base_sha={state_base_sha!r}"
                ),
                exit_code=3,
            )
            return
        state_task_id = task_id_from_card
        state_base_sha = _current_sha()
        state.update(
            {
                "state": STATE_AWAITING_WORK,
                "round": 1,
                "task_id": state_task_id,
                "base_sha": state_base_sha,
                "started_at": _ts(),
                "round_details": [],
            }
        )
        _save_single_round_state()

    if state_task_id != task_id_from_card:
        _fail_single_round(
            outcome="state_task_mismatch",
            message=(
                "task_id mismatch between state.json and task card: "
                f"state={state_task_id!r} task={task_id_from_card!r}"
            ),
            exit_code=3,
        )
        return

    task_id = str(state_task_id)
    base_sha = str(state_base_sha)
    _set_feed_task_id(task_id)

    _log(f"Loaded task card: {task_id}")
    _log(f"Goal: {task_card.get('goal', '<no goal>')}")
    _log(f"Single-round state contract: task_id={task_id} base_sha={base_sha}")

    for stale_key in ("outcome", "failed_at", "error"):
        state.pop(stale_key, None)
    if not isinstance(state.get("round_details"), list):
        state["round_details"] = []
    state["started_at"] = _ts()
    state["round"] = round_num
    state["state"] = STATE_AWAITING_WORK
    _save_single_round_state()

    worker_prompt = _worker_prompt(task_id, round_num)
    _archive_bus_file(WORK_REPORT, task_id, round_num, "work_report")
    WORK_REPORT.unlink(missing_ok=True)
    _archive_bus_file(REVIEW_REPORT, task_id, round_num, "review_report")
    REVIEW_REPORT.unlink(missing_ok=True)

    print(f"\n{'='*60}")
    print(f"  ROUND {round_num}  —  Awaiting Worker")
    print(f"{'='*60}")
    print(f"  Task card: {TASK_CARD}")
    if round_num == 1:
        print("  Send task_card.json to Worker.")
    else:
        print("  Send fix_list.json to Worker.")

    work: dict | None = None
    if auto_dispatch:
        try:
            if dispatch_backend == "par":
                work = _dispatch_with_artifact_fallback(
                    role="worker",
                    dispatch_call=lambda: _run_par_dispatch(
                        role="worker",
                        target=par_worker_target,
                        prompt=worker_prompt,
                        timeout_sec=dispatch_timeout,
                        par_bin=par_bin,
                    ),
                    artifact_path=WORK_REPORT,
                    task_id=task_id,
                    round_num=round_num,
                    timeout_sec=artifact_timeout,
                )
            else:
                work = _dispatch_with_artifact_fallback(
                    role="worker",
                    dispatch_call=lambda: _run_auto_dispatch(
                        role="worker",
                        backend=worker_backend,
                        prompt=worker_prompt,
                        timeout_sec=dispatch_timeout,
                        verbose=verbose,
                    ),
                    artifact_path=WORK_REPORT,
                    task_id=task_id,
                    round_num=round_num,
                    timeout_sec=artifact_timeout,
                )
        except RuntimeError as e:
            _fail_single_round(
                outcome="worker_dispatch_failed",
                message=str(e),
                exit_code=3,
            )
            return

    if work is None:
        work = _wait_for_file(
            WORK_REPORT,
            "Worker result",
            timeout_sec=timeout,
            expected_task_id=task_id,
            expected_round=round_num,
            expected_role="worker" if require_heartbeat else None,
            heartbeat_ttl_sec=heartbeat_ttl,
            show_manual_hint=not auto_dispatch,
        )
    if work is None:
        if require_heartbeat:
            _log("Worker unavailable or timed out. Aborting.")
            print("\n  Worker unavailable or timed out. Check .loop/runtime and logs.")
        else:
            _log("Worker timed out. Aborting.")
            print("\n  Worker did not respond in time. Check .loop/logs/ for details.")
        state["state"] = STATE_DONE
        state["outcome"] = "worker_timeout"
        _save_single_round_state()
        sys.exit(2)

    report_error = _validate_work_report(
        work,
        expected_task_id=task_id,
        expected_round=round_num,
    )
    if report_error:
        _fail_single_round(
            outcome="invalid_work_report",
            message=report_error,
            exit_code=3,
        )
        return

    head_sha = str(work["head_sha"])
    if head_sha == base_sha:
        _fail_single_round(
            outcome="worker_noop",
            message=(
                "Worker reported no code changes: head_sha equals base_sha "
                f"({head_sha}). task_id={task_id} round={round_num}"
            ),
            exit_code=3,
        )
        return

    _log(f"Worker done. head_sha={head_sha}")
    print(f"  Worker completed: {head_sha[:8]}")
    print(f"  Files changed: {', '.join(work.get('files_changed', []))}")

    try:
        diff = _diff(base_sha, head_sha)
        commits = _log_oneline(base_sha, head_sha)
    except RuntimeError as e:
        _fail_single_round(
            outcome="git_compare_failed",
            message=f"Failed to compare commits for base={base_sha} head={head_sha}: {e}",
        )
        return

    review_request = {
        "task_id": task_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "commits": commits,
        "diff": diff,
        "acceptance_criteria": task_card.get("acceptance_criteria", []),
        "constraints": task_card.get("constraints", []),
        "round": round_num,
        "worker_notes": work.get("notes", ""),
        "worker_tests": work.get("tests", []),
    }
    _archive_bus_file(REVIEW_REQ, task_id, round_num, "review_request")
    REVIEW_REQ.write_text(
        json.dumps(review_request, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _archive_bus_file(REVIEW_REPORT, task_id, round_num, "review_report")
    REVIEW_REPORT.unlink(missing_ok=True)

    state["state"] = STATE_AWAITING_REVIEW
    state["head_sha"] = head_sha
    _save_single_round_state()

    print(f"\n{'='*60}")
    print(f"  ROUND {round_num}  —  Awaiting Reviewer")
    print(f"{'='*60}")
    print(f"  Review request: {REVIEW_REQ}")

    review: dict | None = None
    if auto_dispatch:
        try:
            if dispatch_backend == "par":
                review = _dispatch_with_artifact_fallback(
                    role="reviewer",
                    dispatch_call=lambda: _run_par_dispatch(
                        role="reviewer",
                        target=par_reviewer_target,
                        prompt=_reviewer_prompt(task_id, round_num),
                        timeout_sec=dispatch_timeout,
                        par_bin=par_bin,
                    ),
                    artifact_path=REVIEW_REPORT,
                    task_id=task_id,
                    round_num=round_num,
                    timeout_sec=artifact_timeout,
                )
            else:
                review = _dispatch_with_artifact_fallback(
                    role="reviewer",
                    dispatch_call=lambda: _run_auto_dispatch(
                        role="reviewer",
                        backend=reviewer_backend,
                        prompt=_reviewer_prompt(task_id, round_num),
                        timeout_sec=dispatch_timeout,
                        verbose=verbose,
                    ),
                    artifact_path=REVIEW_REPORT,
                    task_id=task_id,
                    round_num=round_num,
                    timeout_sec=artifact_timeout,
                )
        except RuntimeError as e:
            _fail_single_round(
                outcome="reviewer_dispatch_failed",
                message=str(e),
                exit_code=3,
            )
            return

    if review is None:
        review = _wait_for_file(
            REVIEW_REPORT,
            "Reviewer result",
            timeout_sec=timeout,
            expected_task_id=task_id,
            expected_round=round_num,
            expected_role="reviewer" if require_heartbeat else None,
            heartbeat_ttl_sec=heartbeat_ttl,
            show_manual_hint=not auto_dispatch,
        )
    if review is None:
        if require_heartbeat:
            _log("Reviewer unavailable or timed out. Aborting.")
        else:
            _log("Reviewer timed out. Aborting.")
        state["state"] = STATE_DONE
        state["outcome"] = "reviewer_timeout"
        _save_single_round_state()
        sys.exit(2)

    decision = review.get("decision", "changes_required")
    _log(f"Reviewer decision: {decision}")
    print(f"\n  Reviewer: {decision}")

    round_detail = {
        "round": round_num,
        "started_at": state.get("started_at"),
        "worker_notes": work.get("notes", ""),
        "tests_summary": _tests_summary(work.get("tests", [])),
        "review_decision": decision,
    }
    round_details = [
        item for item in state.get("round_details", [])
        if not (isinstance(item, dict) and item.get("round") == round_num)
    ]
    round_details.append(round_detail)
    state["round_details"] = round_details

    if decision == "approve":
        state["state"] = STATE_DONE
        state["outcome"] = "approved"
        _save_single_round_state()
        print(f"\n{'='*60}")
        print(f"  APPROVED at round {round_num}")
        print(f"  base: {base_sha[:8]}  head: {head_sha[:8]}")
        print(f"{'='*60}")

        summary = LOOP_DIR / "summary.json"
        summary.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "outcome": "approved",
                    "rounds": round_num,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "files_changed": work.get("files_changed", []),
                    "review_non_blocking": review.get("non_blocking_suggestions", []),
                    "round_details": state.get("round_details", []),
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        _archive_task_summary(task_id)
        _log("Task approved. Summary written to .loop/summary.json")
        return

    blocking = review.get("blocking_issues", [])
    fix_list = {
        "task_id": task_id,
        "round": round_num + 1,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "fixes": blocking,
        "prior_round_notes": work.get("notes", ""),
        "prior_review_non_blocking": review.get("non_blocking_suggestions", []),
    }
    _archive_bus_file(FIX_LIST, task_id, round_num, "fix_list")
    FIX_LIST.write_text(
        json.dumps(fix_list, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _archive_bus_file(WORK_REPORT, task_id, round_num, "work_report")
    WORK_REPORT.unlink(missing_ok=True)

    print(f"  Blocking issues: {len(blocking)}")
    for issue in blocking:
        print(
            f"    - [{issue.get('severity','?')}] {issue.get('file','')}: "
            f"{issue.get('reason','')}"
        )
    print(f"  Fix list written to {FIX_LIST}")

    state["state"] = STATE_AWAITING_WORK
    state["round"] = round_num + 1
    _save_single_round_state()


def _run_multi_round_via_subprocess(
    *,
    task_path: str,
    max_rounds: int,
    timeout: int,
    require_heartbeat: bool,
    heartbeat_ttl: int,
    auto_dispatch: bool,
    dispatch_backend: str,
    worker_backend: str,
    reviewer_backend: str,
    dispatch_timeout: int,
    artifact_timeout: int,
    par_bin: str,
    par_worker_target: str,
    par_reviewer_target: str,
    allow_dirty: bool,
    verbose: bool,
    worktree_checked: bool = False,
    resume_from_state: dict | None = None,
) -> None:
    if not worktree_checked:
        _enforce_clean_worktree_or_exit(allow_dirty=allow_dirty)

    start_round = 1
    if resume_from_state is None:
        task_card, task_id = _sync_task_card_to_bus(task_path, round_num=1)
        _set_feed_task_id(task_id)

        _log(f"Loaded task card: {task_id}")
        _log(f"Goal: {task_card.get('goal', '<no goal>')}")

        base_sha = _current_sha()
        _log(f"Base SHA: {base_sha}")

        state = _load_state()
        for stale_key in ("outcome", "failed_at", "error", "head_sha", "round_details"):
            state.pop(stale_key, None)
        state.update(
            {
                "state": STATE_AWAITING_WORK,
                "round": 1,
                "task_id": task_id,
                "base_sha": base_sha,
                "started_at": _ts(),
            }
        )
        _save_state(state)
    else:
        state = dict(resume_from_state)
        state_task_id = state.get("task_id")
        state_base_sha = state.get("base_sha")
        state_round = state.get("round")
        if (
            not isinstance(state_task_id, str)
            or not state_task_id
            or not isinstance(state_base_sha, str)
            or not state_base_sha
            or not isinstance(state_round, int)
            or state_round < 1
        ):
            _fail_with_state(
                state,
                outcome="invalid_resume_state",
                message=(
                    "state.json is missing required resume contract "
                    "(task_id/base_sha/round). Re-run without --resume."
                ),
                exit_code=3,
            )
            return
        _, task_card, task_id_from_card = _load_task_card(str(TASK_CARD))
        if task_id_from_card != state_task_id:
            _fail_with_state(
                state,
                outcome="state_task_mismatch",
                message=(
                    "task_id mismatch between state.json and task card during resume: "
                    f"state={state_task_id!r} task={task_id_from_card!r}"
                ),
                exit_code=3,
            )
            return
        task_id = state_task_id
        base_sha = state_base_sha
        start_round = state_round
        _set_feed_task_id(task_id)
        _log(f"Resuming task: {task_id}")
        _log(f"Resume contract: base_sha={base_sha} round={start_round}")
        for stale_key in ("outcome", "failed_at", "error", "head_sha"):
            state.pop(stale_key, None)
        if not isinstance(state.get("round_details"), list):
            state["round_details"] = []
        state["state"] = STATE_AWAITING_WORK
        state["round"] = start_round
        state["started_at"] = _ts()
        _save_state(state)

    _archive_bus_file(WORK_REPORT, task_id, start_round, "work_report")
    WORK_REPORT.unlink(missing_ok=True)
    _archive_bus_file(REVIEW_REPORT, task_id, start_round, "review_report")
    REVIEW_REPORT.unlink(missing_ok=True)
    for role in ("worker", "reviewer"):
        _dispatch_log_path(role).unlink(missing_ok=True)

    last_decision = "changes_required"
    for round_num in range(start_round, max_rounds + 1):
        print(f"\n{'='*60}")
        print(f"  ROUND {round_num}/{max_rounds}  —  Single-Round Subprocess")
        print(f"{'='*60}")
        _archive_bus_file(STATE_FILE, task_id, round_num, "state")

        cmd = _single_round_subprocess_cmd(
            round_num=round_num,
            timeout=timeout,
            require_heartbeat=require_heartbeat,
            heartbeat_ttl=heartbeat_ttl,
            auto_dispatch=auto_dispatch,
            dispatch_backend=dispatch_backend,
            worker_backend=worker_backend,
            reviewer_backend=reviewer_backend,
            dispatch_timeout=dispatch_timeout,
            artifact_timeout=artifact_timeout,
            par_bin=par_bin,
            par_worker_target=par_worker_target,
            par_reviewer_target=par_reviewer_target,
            allow_dirty=allow_dirty,
            verbose=verbose,
        )
        _log(f"Launching single-round subprocess: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        stdout, stderr, returncode = _collect_streamed_text_output(
            proc,
            stdout_line_callback=lambda raw_line: print(raw_line, end="", flush=True),
        )
        result = subprocess.CompletedProcess(
            cmd,
            returncode if returncode is not None else 1,
            stdout,
            stderr,
        )
        if result.returncode != 0:
            if result.stdout:
                _log(f"single-round stdout:\n{result.stdout.rstrip()}")
            if result.stderr:
                _log(f"single-round stderr:\n{result.stderr.rstrip()}")
            _fail_with_state(
                state,
                outcome="single_round_failed",
                message=f"single-round subprocess failed for round={round_num} rc={result.returncode}",
                exit_code=3,
            )
            return

        _ = _load_task_card(str(TASK_CARD))
        review = _read_json_if_exists(REVIEW_REPORT)
        fix_list = _read_json_if_exists(FIX_LIST)
        state = _load_state()

        if state.get("task_id") != task_id or state.get("base_sha") != base_sha:
            _fail_with_state(
                state,
                outcome="state_contract_mismatch",
                message=(
                    "state.json contract mismatch after single-round subprocess: "
                    f"expected task_id={task_id} base_sha={base_sha}, "
                    f"got task_id={state.get('task_id')} base_sha={state.get('base_sha')}"
                ),
                exit_code=3,
            )
            return

        if state.get("state") == STATE_DONE and state.get("outcome") == "approved":
            _archive_task_summary(task_id)
            _log(f"Task approved via state contract at round={round_num}")
            return

        if (
            state.get("state") == STATE_AWAITING_WORK
            and state.get("round") == round_num + 1
        ):
            last_decision = "changes_required"
            if (
                isinstance(fix_list, dict)
                and fix_list.get("task_id") == task_id
                and fix_list.get("round") == round_num + 1
            ):
                blocking = fix_list.get("fixes", [])
                print(f"  Blocking issues: {len(blocking)}")
                for issue in blocking:
                    print(
                        f"    - [{issue.get('severity','?')}] {issue.get('file','')}: "
                        f"{issue.get('reason','')}"
                    )
            else:
                _log(
                    "State indicates changes_required, but fix_list.json is missing/stale; "
                    "continuing based on state.json contract."
                )
            if isinstance(review, dict):
                decision = review.get("decision")
                if decision not in (None, "changes_required"):
                    _log(
                        f"Ignoring stale review_report decision={decision!r}; "
                        "state.json is authoritative."
                    )
            continue

        _fail_with_state(
            state,
            outcome="invalid_state_transition",
            message=(
                "single-round subprocess exited 0 but did not produce a valid state transition: "
                f"state={state.get('state')!r} outcome={state.get('outcome')!r} round={state.get('round')!r}"
            ),
            exit_code=3,
        )
        return

    state = _load_state()
    state["state"] = STATE_DONE
    state["outcome"] = "max_rounds_exhausted"
    _save_state(state)
    print(f"\n  MAX ROUNDS ({max_rounds}) reached without approval.")
    print(f"  Last review decision: {last_decision}")
    print("  PM should re-evaluate task scope or split the task.")
    sys.exit(1)


def cmd_run(
    task_path: str,
    max_rounds: int,
    timeout: int,
    require_heartbeat: bool,
    heartbeat_ttl: int,
    auto_dispatch: bool,
    dispatch_backend: str,
    worker_backend: str,
    reviewer_backend: str,
    dispatch_timeout: int,
    artifact_timeout: int,
    par_bin: str,
    par_worker_target: str,
    par_reviewer_target: str,
    single_round: bool,
    round_num: int | None,
    allow_dirty: bool = False,
    resume: bool = False,
    verbose: bool = False,
) -> None:
    lock: _LoopLock | None = None
    # Single-round subprocesses are spawned by the parent loop which already
    # holds the lock — skip lock acquisition to avoid self-deadlock.
    if not single_round:
        try:
            lock = _acquire_run_lock()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(5)
    try:
        _enforce_clean_worktree_or_exit(allow_dirty=allow_dirty)

        if resume and single_round:
            print("Error: --resume cannot be combined with --single-round", file=sys.stderr)
            sys.exit(1)

        if single_round:
            if round_num is None or round_num < 1:
                print("Error: --single-round requires --round N (N >= 1)", file=sys.stderr)
                sys.exit(1)
            _run_single_round(
                task_path=task_path,
                round_num=round_num,
                timeout=timeout,
                require_heartbeat=require_heartbeat,
                heartbeat_ttl=heartbeat_ttl,
                auto_dispatch=auto_dispatch,
                dispatch_backend=dispatch_backend,
                worker_backend=worker_backend,
                reviewer_backend=reviewer_backend,
                dispatch_timeout=dispatch_timeout,
                artifact_timeout=artifact_timeout,
                par_bin=par_bin,
                par_worker_target=par_worker_target,
                par_reviewer_target=par_reviewer_target,
                verbose=verbose,
            )
            return

        if round_num is not None:
            print("Error: --round is only valid together with --single-round", file=sys.stderr)
            sys.exit(1)

        resume_state: dict | None = None
        if resume:
            resume_state = _load_state()
            outcome = resume_state.get("outcome")
            state_name = resume_state.get("state")
            if state_name == STATE_DONE and outcome == "approved":
                print(
                    "Resume not needed: state.json already marked done/approved "
                    f"for task_id={resume_state.get('task_id')!r}."
                )
                return
            if state_name == STATE_DONE and outcome != "approved":
                error_text = resume_state.get("error") or "<no error details in state.json>"
                print(
                    "Error: cannot resume because state.json indicates a failed run: "
                    f"outcome={outcome!r} error={error_text}",
                    file=sys.stderr,
                )
                print("Re-run without --resume to start a fresh run.", file=sys.stderr)
                sys.exit(3)

        _run_multi_round_via_subprocess(
            task_path=task_path,
            max_rounds=max_rounds,
            timeout=timeout,
            require_heartbeat=require_heartbeat,
            heartbeat_ttl=heartbeat_ttl,
            auto_dispatch=auto_dispatch,
            dispatch_backend=dispatch_backend,
            worker_backend=worker_backend,
            reviewer_backend=reviewer_backend,
            dispatch_timeout=dispatch_timeout,
            artifact_timeout=artifact_timeout,
            par_bin=par_bin,
            par_worker_target=par_worker_target,
            par_reviewer_target=par_reviewer_target,
            allow_dirty=allow_dirty,
            verbose=verbose,
            worktree_checked=True,
            resume_from_state=resume_state,
        )
    finally:
        if lock is not None:
            lock.release()


# ── CLI ─────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="PM-driven review loop orchestrator",
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--loop-dir",
        default=".loop",
        help="Loop bus directory (relative values resolve from repo root)",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("init", parents=[shared], help="Create loop directory structure")

    sub.add_parser("status", parents=[shared], help="Show current loop state")

    health_p = sub.add_parser("health", parents=[shared], help="Show worker/reviewer heartbeat health")
    health_p.add_argument("--ttl", type=int, default=DEFAULT_HEARTBEAT_TTL_SEC,
                          help="Heartbeat freshness threshold in seconds")

    hb_p = sub.add_parser("heartbeat", parents=[shared], help="Write role heartbeat continuously")
    hb_p.add_argument("--role", choices=["worker", "reviewer"], required=True)
    hb_p.add_argument("--interval", type=int, default=5,
                      help="Heartbeat write interval in seconds")

    diff_p = sub.add_parser("extract-diff", parents=[shared], help="Print git diff between two commits")
    diff_p.add_argument("base")
    diff_p.add_argument("head")

    archive_p = sub.add_parser("archive", parents=[shared], help="List or restore archived bus files")
    archive_p.add_argument("--task-id", required=True, help="Task ID archive key (e.g. T-604)")
    archive_p.add_argument(
        "--restore",
        help="Archive file stem/name to restore into current loop dir (e.g. r1_work_report)",
    )

    run_p = sub.add_parser("run", parents=[shared], help="Run the full PM-controlled review loop")
    run_p.add_argument("--task", default=None,
                       help="Path to task card JSON")
    run_p.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                       help="Maximum review rounds (default: 3)")
    run_p.add_argument("--timeout", type=int, default=0,
                       help="Per-phase timeout in seconds (0=unlimited)")
    run_p.add_argument("--require-heartbeat", action="store_true",
                       help="Require fresh worker/reviewer heartbeat while waiting")
    run_p.add_argument("--heartbeat-ttl", type=int, default=DEFAULT_HEARTBEAT_TTL_SEC,
                       help="Heartbeat freshness threshold in seconds")
    run_p.add_argument("--auto-dispatch", action="store_true",
                       help="Automatically invoke worker/reviewer backends each round")
    run_p.add_argument("--dispatch-backend", choices=["native", "par"],
                       default=DEFAULT_DISPATCH_BACKEND,
                       help="Dispatch transport: native subprocess calls or par send")
    run_p.add_argument("--worker-backend", default="codex",
                       help="Backend used for auto worker dispatch (native mode)")
    run_p.add_argument("--reviewer-backend", default="codex",
                       help="Backend used for auto reviewer dispatch (native mode)")
    run_p.add_argument("--dispatch-timeout", type=int, default=DEFAULT_DISPATCH_TIMEOUT_SEC,
                       help="Per-dispatch timeout in seconds (default: 600, 0=unlimited)")
    run_p.add_argument("--artifact-timeout", type=int, default=DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
                       help="Post-dispatch artifact timeout in seconds (default: 90)")
    run_p.add_argument("--par-bin", default="par",
                       help="Par executable name or absolute path (par mode)")
    run_p.add_argument("--par-worker-target", default="worker",
                       help="Par target/session name for worker role (par mode)")
    run_p.add_argument("--par-reviewer-target", default="reviewer",
                       help="Par target/session name for reviewer role (par mode)")
    run_p.add_argument("--single-round", action="store_true",
                       help="Run exactly one round and exit")
    run_p.add_argument("--round", type=int,
                       help="Round number for --single-round mode")
    run_p.add_argument("--allow-dirty", action="store_true",
                       help="Allow run to start with dirty tracked git files")
    run_p.add_argument("--resume", action="store_true",
                       help="Resume from .loop/state.json contract")
    run_p.add_argument("--verbose", action="store_true",
                       help="Stream full backend stdout lines during auto-dispatch")

    args = parser.parse_args()
    if args.cmd is None:
        parser.print_help()
        return
    _configure_loop_paths(args.loop_dir)
    if args.cmd == "init":
        cmd_init()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "health":
        cmd_health(args.ttl)
    elif args.cmd == "heartbeat":
        cmd_heartbeat(args.role, args.interval)
    elif args.cmd == "extract-diff":
        cmd_extract_diff(args.base, args.head)
    elif args.cmd == "archive":
        cmd_archive(args.task_id, args.restore)
    elif args.cmd == "run":
        task_path = args.task if args.task is not None else str(TASK_CARD)
        cmd_run(task_path, args.max_rounds, args.timeout,
                args.require_heartbeat, args.heartbeat_ttl,
                args.auto_dispatch, args.dispatch_backend,
                args.worker_backend, args.reviewer_backend,
                args.dispatch_timeout, args.artifact_timeout, args.par_bin,
                args.par_worker_target, args.par_reviewer_target,
                args.single_round, args.round, args.allow_dirty, args.resume, args.verbose)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
