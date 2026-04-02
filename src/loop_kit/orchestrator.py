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
import contextlib
import importlib.metadata
import importlib.resources
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

ROOT = Path.cwd()
LOOP_DIR = ROOT / ".loop"
LOGS_DIR = LOOP_DIR / "logs"
RUNTIME_DIR = LOOP_DIR / "runtime"
ARCHIVE_DIR = LOOP_DIR / "archive"
STATE_FILE = LOOP_DIR / "state.json"

DEFAULT_MAX_ROUNDS = 3
POLL_INTERVAL_SEC = 1
DEFAULT_HEARTBEAT_TTL_SEC = 30
DEFAULT_DISPATCH_TIMEOUT_SEC = 600
DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC = 90
DEFAULT_DISPATCH_RETRIES = 2
DEFAULT_DISPATCH_RETRY_BASE_SEC = 5
MAX_DISPATCH_RETRY_DELAY_SEC = 60
DEFAULT_GIT_TIMEOUT_SEC = 30
BACKEND_CODEX = "codex"
BACKEND_CLAUDE = "claude"
BACKEND_OPENCODE = "opencode"
DISPATCH_BACKEND_NATIVE = "native"
DEFAULT_WORKER_BACKEND = BACKEND_CODEX
DEFAULT_REVIEWER_BACKEND = BACKEND_CODEX
DEFAULT_DISPATCH_BACKEND = DISPATCH_BACKEND_NATIVE
DISPATCH_STREAM_POLL_SEC = 0.1
_WAIT_SAFETY_CAP_SEC = 86400  # 24h absolute cap in _wait_for_file
EXIT_OK = 0
EXIT_GENERAL_ERROR = 1
EXIT_TIMEOUT = 2
EXIT_VALIDATION_ERROR = 3
EXIT_DIRTY_WORKTREE = 4
EXIT_LOCK_FAILURE = 5
EXIT_INTERRUPTED = 130
_FEED_TASK_ID: str | None = None
_LOGS_DIR_ENSURED = False
_LOGS_DIR_ENSURED_PATH: str | None = None
_stream_local = threading.local()


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


@dataclass(slots=True)
class RunConfig:
    task_path: str = field(default_factory=lambda: str(TASK_CARD))
    max_rounds: int = DEFAULT_MAX_ROUNDS
    timeout: int = 0
    require_heartbeat: bool = False
    heartbeat_ttl: int = DEFAULT_HEARTBEAT_TTL_SEC
    auto_dispatch: bool = False
    dispatch_backend: str = DEFAULT_DISPATCH_BACKEND
    worker_backend: str = DEFAULT_WORKER_BACKEND
    reviewer_backend: str = DEFAULT_REVIEWER_BACKEND
    dispatch_timeout: int = DEFAULT_DISPATCH_TIMEOUT_SEC
    dispatch_retries: int = DEFAULT_DISPATCH_RETRIES
    dispatch_retry_base_sec: int = DEFAULT_DISPATCH_RETRY_BASE_SEC
    artifact_timeout: int = DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC
    allow_dirty: bool = False
    verbose: bool = False


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
    global _LOGS_DIR_ENSURED
    global _LOGS_DIR_ENSURED_PATH

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
    _LOGS_DIR_ENSURED = False
    _LOGS_DIR_ENSURED_PATH = None


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


def _prepare_bus_file(path: Path, task_id: str, round_num: int, suffix: str) -> None:
    _archive_bus_file(path, task_id, round_num, suffix)
    path.unlink(missing_ok=True)


def _clean_stale_state(state: dict, *keys: str) -> None:
    for key in keys:
        state.pop(key, None)


def _close_pipe(pipe: object | None) -> None:
    if pipe is None:
        return
    close = getattr(pipe, "close", None)
    if callable(close):
        with contextlib.suppress(OSError):
            close()


def _completed_proc(
    cmd: list[str],
    returncode: int | None,
    stdout: str,
    stderr: str,
    *,
    default_returncode: int = 1,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        cmd,
        returncode if returncode is not None else default_returncode,
        stdout,
        stderr,
    )


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


def _lock_file(handle) -> None:
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _LoopLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
        except OSError as e:
            handle.close()
            raise RuntimeError(f"another orchestrator instance is already running ({self.path})") from e
        try:
            _lock_file(handle)
            self._handle = handle
        except OSError as e:
            handle.close()
            raise RuntimeError(f"another orchestrator instance is already running ({self.path})") from e
        except Exception:
            handle.close()
            raise

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            _unlock_file(handle)
        finally:
            handle.close()

    def __enter__(self) -> "_LoopLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
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
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_feed_task_id(task_id: str | None) -> None:
    global _FEED_TASK_ID
    _FEED_TASK_ID = task_id


def _ensure_logs_dir() -> None:
    global _LOGS_DIR_ENSURED
    global _LOGS_DIR_ENSURED_PATH
    current_logs_dir = _normalized_abs(LOGS_DIR)
    if _LOGS_DIR_ENSURED and current_logs_dir == _LOGS_DIR_ENSURED_PATH and LOGS_DIR.is_dir():
        return
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _LOGS_DIR_ENSURED = True
    _LOGS_DIR_ENSURED_PATH = current_logs_dir


def _feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
    if _FEED_TASK_ID and data and data.get("task_id") not in (None, _FEED_TASK_ID):
        return
    payload_data = dict(data or {})
    if _FEED_TASK_ID and "task_id" not in payload_data:
        payload_data["task_id"] = _FEED_TASK_ID
    _ensure_logs_dir()
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
    _ensure_logs_dir()
    entry: dict[str, object] = {"ts": ts, "msg": msg}
    if _FEED_TASK_ID:
        entry["task_id"] = _FEED_TASK_ID
    with open(LOGS_DIR / "orchestrator.log", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _feed_event("log", data={"message": msg})


def _normalized_abs(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _read_json_if_exists(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _log(f"Warning: {path.name} has invalid JSON: {e}")
        return None
    except OSError:
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
        return command.strip()
    if isinstance(command, list):
        rendered = " ".join(str(part) for part in command if isinstance(part, (str, int, float)))
        if rendered.strip():
            return rendered.strip()
    call = item.get("call")
    if isinstance(call, dict):
        return _extract_command_summary(call)
    return ""


def _extract_file_paths(item: dict) -> list[str]:
    found: list[str] = []

    def _append_path(value: object) -> None:
        if not isinstance(value, str):
            return
        normalized = value.strip()
        if not normalized:
            return
        if normalized not in found:
            found.append(normalized)

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for key in (
                "path",
                "file",
                "filepath",
                "file_path",
                "relative_path",
                "absolute_path",
                "target_path",
            ):
                _append_path(value.get(key))
            for key in (
                "paths",
                "files",
                "file_paths",
                "changes",
                "edits",
                "items",
                "entries",
            ):
                nested = value.get(key)
                if isinstance(nested, (dict, list)):
                    _walk(nested)
            return
        if isinstance(value, list):
            for item_value in value:
                _walk(item_value)

    _walk(item)
    return found


def _summarize_paths(paths: list[str], max_items: int = 3) -> str:
    if not paths:
        return ""
    if len(paths) <= max_items:
        return ", ".join(paths)
    head = ", ".join(paths[:max_items])
    remaining = len(paths) - max_items
    return f"{head} (+{remaining} more)"


def _strip_outer_quotes(text: str) -> str:
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
        return stripped[1:-1].strip()
    return stripped


def _strip_powershell_wrapper(command_text: str) -> str:
    stripped = command_text.strip()
    lowered = stripped.lower()
    marker = " -command "
    marker_index = lowered.find(marker)
    if marker_index <= 0:
        return _strip_outer_quotes(stripped)
    launcher = _strip_outer_quotes(stripped[:marker_index].strip())
    launcher_name = launcher.replace("\\", "/").split("/")[-1].lower()
    if launcher_name not in {"pwsh", "pwsh.exe", "powershell", "powershell.exe"}:
        return _strip_outer_quotes(stripped)
    inner = _strip_outer_quotes(stripped[marker_index + len(marker) :].strip())
    return inner or _strip_outer_quotes(stripped)


def _clean_path_text(path_text: str) -> str:
    cleaned = path_text.strip().strip("\"'")
    if "|" in cleaned:
        cleaned = cleaned.split("|", 1)[0].strip()
    return cleaned.rstrip(";,")


def _path_parts(path_text: str) -> list[str]:
    cleaned = _clean_path_text(path_text)
    if not cleaned:
        return []
    normalized = cleaned.replace("\\", "/").strip()
    return [part for part in normalized.split("/") if part and part != "."]


def _short_filename(path_text: str) -> str:
    cleaned = _clean_path_text(path_text)
    if not cleaned:
        return ""
    parts = _path_parts(cleaned)
    name = parts[-1] if parts else Path(cleaned).name
    return name or cleaned


def _shorten_paths(paths: list[str]) -> list[str]:
    path_parts: list[list[str]] = []
    shortened: list[str] = []
    indexes_by_name: dict[str, list[int]] = {}

    for path_text in paths:
        parts = _path_parts(path_text)
        name = parts[-1] if parts else _short_filename(path_text)
        if not name:
            continue
        index = len(shortened)
        shortened.append(name)
        path_parts.append(parts)
        indexes_by_name.setdefault(name, []).append(index)

    for indexes in indexes_by_name.values():
        if len(indexes) < 2:
            continue
        depth = 2
        while True:
            seen: set[str] = set()
            has_collision = False
            for index in indexes:
                parts = path_parts[index]
                if not parts:  # noqa: SIM108
                    candidate = shortened[index]
                else:
                    candidate = "/".join(parts[-min(depth, len(parts)) :])
                if candidate in seen:
                    has_collision = True
                    break
                seen.add(candidate)
            if not has_collision:
                break
            if all(len(path_parts[index]) <= depth for index in indexes):
                break
            depth += 1
        for index in indexes:
            parts = path_parts[index]
            if not parts:
                continue
            shortened[index] = "/".join(parts[-min(depth, len(parts)) :])
    return shortened


def _codex_event_summary(role: str, backend: str, line: str) -> str | None:
    if backend != BACKEND_CODEX:
        return None

    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload_type = payload.get("type")
    if payload_type == "thread.started":
        thread_id = payload.get("thread_id")
        if isinstance(thread_id, str) and thread_id.strip():
            return f"[{role}] Session: {thread_id.strip()}"
        return f"[{role}] Session started"
    if payload_type == "turn.started":
        return f"[{role}] Turn started"
    if payload_type == "turn.completed":
        return f"[{role}] Turn completed"
    if payload_type == "file_change":
        paths = _shorten_paths(_extract_file_paths(payload))
        return f"[{role}] Editing: {_summarize_paths(paths)}" if paths else f"[{role}] Editing files"
    if payload_type not in ("item.started", "item.completed"):
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if payload_type == "item.started":
        return None
    if item_type == "command_execution":
        command = _extract_command_summary(item)
        if command:
            command = _truncate_summary_text(_strip_powershell_wrapper(command))
        return f"[{role}] Running: {command}" if command else f"[{role}] Running command"
    if item_type == "agent_message":
        message = _flatten_text_payload(item)
        return f"[{role}] Message: {_truncate_summary_text(message)}" if message else f"[{role}] Message"
    if item_type == "file_change":
        paths = _shorten_paths(_extract_file_paths(item))
        return f"[{role}] Editing: {_summarize_paths(paths)}" if paths else f"[{role}] Editing files"
    return None


def _extract_read_filename(command_text: str) -> str | None:
    command = _strip_powershell_wrapper(command_text)
    if not command:
        return None
    try:
        import shlex

        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None
    if tokens[0] == "&" and len(tokens) > 1:
        tokens = tokens[1:]
    if not tokens:
        return None
    first = _strip_outer_quotes(tokens[0]).replace("\\", "/").split("/")[-1].lower()
    if first not in {"get-content", "cat"}:
        return None

    path_token = ""
    idx = 1
    while idx < len(tokens):
        token = tokens[idx]
        lowered = token.lower()
        if first == "get-content" and lowered in {"-path", "-literalpath"}:
            if idx + 1 < len(tokens):
                path_token = tokens[idx + 1]
            break
        if lowered.startswith("-"):
            idx += 1
            continue
        path_token = token
        break
    if not path_token:
        return None
    cleaned = _strip_outer_quotes(path_token).strip().strip("\"'").rstrip(";,")
    if "|" in cleaned:
        cleaned = cleaned.split("|", 1)[0].strip()
    if not cleaned:
        return None
    normalized = cleaned.replace("\\", "/").rstrip("/")
    name = normalized.split("/")[-1] if normalized else ""
    return name or cleaned


def _stream_dispatch_stdout_line(
    role: str,
    backend: str,
    raw_line: str,
    parse_event_fn: "BackendParseEventFn",
    *,
    verbose: bool,
) -> None:

    read_state = getattr(_stream_local, "read_state", None)
    if read_state is None:
        read_state = {}
        _stream_local.read_state = read_state

    state_key = f"{role}:{backend}"
    line = raw_line.rstrip("\r\n")
    summary = parse_event_fn(role, backend, line)
    if not summary:
        read_state.pop(state_key, None)
        return

    read_summary: str | None = None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and payload.get("type") == "item.completed":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution":
            command_text = _extract_command_summary(item)
            read_name = _extract_read_filename(command_text) if command_text else None
            if read_name:
                read_summary = f"[{role}] Reading: {read_name}"

    if read_summary is not None:
        if read_state.get(state_key) == read_summary:
            return
        print(read_summary, flush=True)
        read_state[state_key] = read_summary
        return

    print(summary, flush=True)
    read_state.pop(state_key, None)


BackendBuildFn = Callable[[str, str], tuple[list[str], str | None, str | None]]
BackendResolveFn = Callable[[str], str]
BackendParseEventFn = Callable[[str, str, str], str | None]
_BACKEND_REGISTRY: dict[str, tuple[BackendBuildFn, BackendResolveFn, BackendParseEventFn]] = {}


def _available_backends() -> list[str]:
    return sorted(_BACKEND_REGISTRY.keys())


def register_backend(
    name: str,
    build_cmd_fn: BackendBuildFn,
    resolve_exe_fn: BackendResolveFn,
    parse_event_fn: BackendParseEventFn,
) -> None:
    backend = name.strip().lower()
    if not backend:
        raise ValueError("backend name must not be empty")
    _BACKEND_REGISTRY[backend] = (build_cmd_fn, resolve_exe_fn, parse_event_fn)


def _require_registered_backend(
    backend: str,
) -> tuple[BackendBuildFn, BackendResolveFn, BackendParseEventFn]:
    key = backend.strip().lower()
    spec = _BACKEND_REGISTRY.get(key)
    if spec is None:
        raise ValueError(
            f"Unsupported backend: {backend}. Registered backends: {', '.join(_available_backends()) or '<none>'}"
        )
    return spec


def _resolve_exe_from_candidates(*, backend: str, candidates: list[str | None]) -> str:
    for exe in candidates:
        if exe and Path(exe).exists():
            return exe
    raise RuntimeError(f"Cannot find executable for backend={backend}")


def _claude_parse_event(role: str, backend: str, line: str) -> str | None:
    _ = (role, backend, line)
    return None


def _resolve_codex_exe(backend: str) -> str:
    home = Path.home()
    return _resolve_exe_from_candidates(
        backend=backend,
        candidates=[
            shutil.which(BACKEND_CODEX),
            shutil.which(f"{BACKEND_CODEX}.cmd"),
            # Windows npm global
            str(home / "AppData" / "Roaming" / "npm" / f"{BACKEND_CODEX}.cmd"),
            str(home / "AppData" / "Roaming" / "npm" / BACKEND_CODEX),
            # Unix npm global
            str(home / ".npm-global" / "bin" / BACKEND_CODEX),
            str(home / ".local" / "bin" / BACKEND_CODEX),
            f"/usr/local/bin/{BACKEND_CODEX}",
        ],
    )


def _resolve_claude_exe(backend: str) -> str:
    home = Path.home()
    return _resolve_exe_from_candidates(
        backend=backend,
        candidates=[
            shutil.which(BACKEND_CLAUDE),
            shutil.which(f"{BACKEND_CLAUDE}.exe"),
            # Windows
            str(home / "AppData" / "Local" / "Programs" / BACKEND_CLAUDE / f"{BACKEND_CLAUDE}.exe"),
            str(home / ".local" / "bin" / f"{BACKEND_CLAUDE}.exe"),
            # Unix
            str(home / ".local" / "bin" / BACKEND_CLAUDE),
            f"/usr/local/bin/{BACKEND_CLAUDE}",
        ],
    )


def _build_codex_command(exe: str, prompt: str) -> tuple[list[str], str | None, str | None]:
    return (
        [
            exe,
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(ROOT),
            (
                "Execute the context provided via stdin.  Follow the instructions"
                " embedded in it and only finish after the required output artifact"
                " is written."
            ),
        ],
        None,
        prompt,
    )


def _build_claude_command(exe: str, prompt: str) -> tuple[list[str], str | None, str | None]:
    sid = str(uuid.uuid4())
    return (
        [
            exe,
            "-p",
            "--dangerously-skip-permissions",
            "--session-id",
            sid,
            prompt,
        ],
        sid,
        None,
    )


def _resolve_backend_exe(backend: str) -> str:
    _, resolve_exe_fn, _ = _require_registered_backend(backend)
    return resolve_exe_fn(backend.strip().lower())


def _agent_command(backend: str, prompt: str) -> tuple[list[str], str | None, str | None]:
    """Return (cmd, session_id, stdin_text).

    For codex >= 0.118.0 the prompt context is piped via stdin so the
    command line stays short.  The short CLI arg is a one-line instruction.
    """
    build_cmd_fn, _, _ = _require_registered_backend(backend)
    exe = _resolve_backend_exe(backend)
    return build_cmd_fn(exe, prompt)


def _require_registered_parse_event(backend: str) -> BackendParseEventFn:
    _, _, parse_event_fn = _require_registered_backend(backend)
    return parse_event_fn


def _resolve_opencode_exe(backend: str) -> str:
    home = Path.home()
    return _resolve_exe_from_candidates(
        backend=backend,
        candidates=[
            shutil.which(BACKEND_OPENCODE),
            shutil.which(f"{BACKEND_OPENCODE}.cmd"),
            # Windows npm global
            str(home / "AppData" / "Roaming" / "npm" / f"{BACKEND_OPENCODE}.cmd"),
            str(home / "AppData" / "Roaming" / "npm" / BACKEND_OPENCODE),
            # Unix npm global
            str(home / ".npm-global" / "bin" / BACKEND_OPENCODE),
            str(home / ".local" / "bin" / BACKEND_OPENCODE),
            f"/usr/local/bin/{BACKEND_OPENCODE}",
        ],
    )


def _build_opencode_command(exe: str, prompt: str) -> tuple[list[str], str | None, str | None]:
    return (
        [
            exe,
            "run",
            "--format",
            "json",
            (
                "Execute the context provided via stdin.  Follow the instructions"
                " embedded in it and only finish after the required output artifact"
                " is written."
            ),
        ],
        None,
        prompt,
    )


def _opencode_parse_event(role: str, backend: str, line: str) -> str | None:
    _ = backend
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    part = payload.get("part")
    if not isinstance(part, dict):
        return None
    if event_type == "step_start":
        session_id = part.get("sessionID", "")
        if isinstance(session_id, str) and session_id.strip():
            return f"[{role}] Session: {session_id.strip()}"
        return f"[{role}] Session started"
    if event_type == "text":
        text = part.get("text", "")
        if isinstance(text, str) and text.strip():
            return f"[{role}] Message: {_truncate_summary_text(text)}"
        return None
    if event_type == "tool_use":
        state = part.get("state")
        tool_name = part.get("tool", "")
        if not isinstance(state, dict):
            return None
        status = state.get("status")
        inp = state.get("input")
        if status == "error":
            return None
        if tool_name == "write" and isinstance(inp, dict):
            fp = inp.get("filePath", "")
            if fp:
                return f"[{role}] Editing: {_short_filename(str(fp))}"
            return f"[{role}] Editing files"
        if tool_name == "read" and isinstance(inp, dict):
            fp = inp.get("filePath", "")
            if fp:
                return f"[{role}] Reading: {_short_filename(str(fp))}"
            return f"[{role}] Reading file"
        if tool_name in ("bash", "shell") and isinstance(inp, dict):
            cmd_text = inp.get("command", "")
            if cmd_text:
                return f"[{role}] Running: {_truncate_summary_text(str(cmd_text))}"
            return f"[{role}] Running command"
        if tool_name:
            return f"[{role}] Tool: {tool_name}"
        return None
    if event_type == "step_finish":
        return f"[{role}] Step completed"
    return None


register_backend(BACKEND_CODEX, _build_codex_command, _resolve_codex_exe, _codex_event_summary)
register_backend(BACKEND_CLAUDE, _build_claude_command, _resolve_claude_exe, _claude_parse_event)
register_backend(BACKEND_OPENCODE, _build_opencode_command, _resolve_opencode_exe, _opencode_parse_event)


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
        if backend == BACKEND_CODEX:
            hints.append("check codex API key/login.")
        elif backend == BACKEND_CLAUDE:
            hints.append("check claude authentication/session.")
        else:
            hints.append("check backend authentication.")
    if any(token in lowered for token in ("not found", "no such file", "cannot find")):
        hints.append("verify backend executable path and installation.")
    if not hints:
        hints.append("check backend auth/network and retry.")
    return " Remediation: " + " ".join(hints)


def _report_dispatch_result(
    *,
    role: str,
    backend: str,
    cmd: list[str],
    result: subprocess.CompletedProcess[str],
    attempt: int,
    max_attempts: int,
    session_id: str | None = None,
    stdout_len: int | None = None,
    timeout_sec: int | None = None,
    interrupted: bool = False,
) -> None:
    _write_dispatch_log(role, cmd, result, session_id)
    data = {
        "role": role,
        "mode": DISPATCH_BACKEND_NATIVE,
        "backend": backend,
        "returncode": result.returncode,
        "attempt": attempt,
        "max_attempts": max_attempts,
    }
    if timeout_sec is not None:
        data["timeout_sec"] = timeout_sec
    if session_id is not None:
        data["session_id"] = session_id
    if stdout_len is not None:
        data["stdout_len"] = stdout_len
    if interrupted:
        data["interrupted"] = True
    _feed_event(
        "dispatch_summary",
        level=("info" if timeout_sec is None and result.returncode == 0 else "error"),
        data=data,
    )


def _collect_streamed_process_output(
    proc: subprocess.Popen[str],
    *,
    role: str,
    backend: str,
    parse_event_fn: BackendParseEventFn,
    stdin_text: str | None,
    timeout_sec: int,
    verbose: bool,
) -> tuple[str, str, int, bool]:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdin_thread: threading.Thread | None = None

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
                parse_event_fn,
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
            proc.terminate()
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


def _terminate_subprocess_on_interrupt(proc: subprocess.Popen[str], *, context: str) -> None:
    _close_pipe(getattr(proc, "stdin", None))

    is_running = False
    try:
        is_running = proc.poll() is None
    except OSError:
        is_running = False

    if is_running:
        with contextlib.suppress(OSError):
            proc.terminate()
    with contextlib.suppress(OSError):
        proc.wait()
    status = "terminated" if is_running else "already exited"
    _log(f"Interrupted by SIGINT; subprocess {status} ({context})")


_PERMANENT_DISPATCH_PATTERNS: tuple[str, ...] = (
    "not found",
    "authentication",
    "unauthorized",
    "invalid api key",
    "permission denied",
)


def _is_permanent_dispatch_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(pattern in lowered for pattern in _PERMANENT_DISPATCH_PATTERNS)


def _run_auto_dispatch(
    role: str,
    backend: str,
    prompt: str,
    timeout_sec: int,
    *,
    verbose: bool = False,
    dispatch_retries: int = DEFAULT_DISPATCH_RETRIES,
    dispatch_retry_base_sec: int = DEFAULT_DISPATCH_RETRY_BASE_SEC,
) -> None:
    parse_event_fn = _require_registered_parse_event(backend)
    retry_count = max(0, int(dispatch_retries))
    retry_base_sec = max(1, int(dispatch_retry_base_sec))
    max_attempts = retry_count + 1
    _log(f"Auto-dispatch start: role={role} backend={backend} retries={retry_count} retry_base_sec={retry_base_sec}")
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
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
        try:
            stdout, stderr, returncode, timed_out = _collect_streamed_process_output(
                proc,
                role=role,
                backend=backend,
                parse_event_fn=parse_event_fn,
                stdin_text=stdin_text,
                timeout_sec=timeout_sec,
                verbose=verbose,
            )
        except KeyboardInterrupt:
            _terminate_subprocess_on_interrupt(
                proc,
                context=f"auto-dispatch role={role} backend={backend} attempt={attempt}",
            )
            _report_dispatch_result(
                role=role,
                backend=backend,
                cmd=cmd,
                result=_completed_proc(
                    cmd,
                    proc.returncode,
                    "",
                    "",
                    default_returncode=130,
                ),
                attempt=attempt,
                max_attempts=max_attempts,
                session_id=cmd_sid,
                interrupted=True,
            )
            raise
        if timed_out:
            result = _completed_proc(
                cmd,
                returncode,
                stdout,
                stderr,
                default_returncode=-9,
            )
            _report_dispatch_result(
                role=role,
                backend=backend,
                cmd=cmd,
                result=result,
                attempt=attempt,
                max_attempts=max_attempts,
                session_id=cmd_sid,
                timeout_sec=timeout_sec,
            )
            raise DispatchTimeoutError(
                f"{role} dispatch timeout after {timeout_sec}s (backend={backend})."
                + _dispatch_failure_hint(backend=backend, stderr=stderr or "", timeout=True)
            )
        result = _completed_proc(
            cmd,
            returncode,
            stdout,
            stderr,
        )

        session_id = cmd_sid
        if backend == BACKEND_CODEX:
            parsed = _extract_codex_thread_id(result.stdout or "")
            if parsed:
                session_id = parsed
        _report_dispatch_result(
            role=role,
            backend=backend,
            cmd=cmd,
            result=result,
            attempt=attempt,
            max_attempts=max_attempts,
            session_id=session_id,
            stdout_len=len(result.stdout or ""),
        )

        if result.returncode == 0:
            _log(f"Auto-dispatch done: role={role} backend={backend} attempts={attempt}")
            return

        stderr_text = (result.stderr or "").strip()
        if _is_permanent_dispatch_error(stderr_text):
            raise RuntimeError(
                f"{role} dispatch failed with permanent error (backend={backend}, rc={result.returncode}): "
                f"{stderr_text} — permanent error, not retrying."
                + _dispatch_failure_hint(backend=backend, stderr=stderr_text)
            )
        if attempt >= max_attempts:
            raise RuntimeError(
                f"{role} dispatch failed (backend={backend}, rc={result.returncode}) "
                f"after {attempt} attempts: {stderr_text}" + _dispatch_failure_hint(backend=backend, stderr=stderr_text)
            )
        retry_delay = min(MAX_DISPATCH_RETRY_DELAY_SEC, retry_base_sec * (2 ** (attempt - 1)))
        _log(
            f"{role} dispatch failed (backend={backend}, rc={result.returncode}) on attempt "
            f"{attempt}/{max_attempts}; retrying in {retry_delay}s"
        )
        time.sleep(retry_delay)


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
        _log(f"{role} dispatch timed out; checking {artifact_path.name} for task_id={task_id} round={round_num}")
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
    if artifact_path.exists():
        data = _read_json_if_exists(artifact_path)
        if isinstance(data, dict):
            if data.get("task_id") == task_id and data.get("round") == round_num:
                _log(f"{role} dispatch produced {artifact_path.name} directly; skipping wait")
                return data
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
    except (OSError, UnicodeDecodeError):
        return None


def _as_prompt_list(items: object) -> str:
    if not isinstance(items, list) or not items:
        return "- <none>"
    return "\n".join(f"- {item}" for item in items)


_function_index_cache: tuple[tuple[int, float], str] | None = None


def _function_index(path: Path) -> str:
    global _function_index_cache
    try:
        stat = path.stat()
    except OSError:
        return "- <unavailable>"

    key = (stat.st_mtime_ns, stat.st_size)
    if _function_index_cache is not None and _function_index_cache[0] == key:
        return _function_index_cache[1]

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "- <unavailable>"

    entries: list[str] = []
    for line_no, raw_line in enumerate(lines, start=1):
        stripped = raw_line.lstrip()
        if stripped.startswith(("def ", "async def ", "class ")):
            entries.append(f"- L{line_no}: {stripped}")

    if not entries:
        result = "- <none>"
    else:
        result = "\n".join(entries)

    _function_index_cache = (key, result)
    return result


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
    "=== BEGIN FUNCTION INDEX: {orchestrator_path} ===\n"
    "{function_index}\n"
    "=== END FUNCTION INDEX ===\n\n"
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
            f"Missing required prompt template: {_display_path(template_path)}. Run 'loop init' to create it."
        )
    try:
        return template_text.format(**context)
    except (KeyError, ValueError) as e:
        raise RuntimeError(f"Invalid prompt template at {_display_path(template_path)}: {e}") from e


def _read_required_text(path: Path, *, label: str) -> str:
    text = _read_text_optional(path)
    if text:
        return text
    raise RuntimeError(f"Missing required {label}: {_display_path(path)}. Create this file and re-run.")


def _read_text_with_default(project_path: Path, default_filename: str) -> str:
    project_text = _read_text_optional(project_path)
    if project_text:
        return project_text

    fallback_path = Path(__file__).resolve().parent / "defaults" / default_filename
    default_text: str | None = None

    try:
        default_resource = importlib.resources.files("loop_kit.defaults").joinpath(default_filename)
        default_text = default_resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        default_text = _read_text_optional(fallback_path)

    if default_text:
        return default_text

    raise RuntimeError(
        "Missing default prompt context content: "
        f"{default_filename} (project override missing at {_display_path(project_path)})."
    )


def _render_fix_list_section(round_num: int) -> str:
    fix_list = _read_json_if_exists(FIX_LIST)
    if not isinstance(fix_list, dict):
        return "- <none>"
    fixes = fix_list.get("fixes", [])
    if not isinstance(fixes, list) or not fixes:
        return "- <none>"
    lines = []
    for issue in fixes:
        if not isinstance(issue, dict):
            continue
        severity = issue.get("severity", "?")
        file = issue.get("file", "")
        reason = issue.get("reason", "")
        lines.append(f"- [{severity}] {file}: {reason}")
    return "\n".join(lines) if lines else "- <none>"


def _worker_prompt(task_id: str, round_num: int) -> str:
    if round_num > 1:
        role_text = _read_text_with_default(
            ROOT / "docs" / "roles" / "code-writer.md",
            "code_writer_md_default.txt",
        )
        fix_list_section = _render_fix_list_section(round_num)
        prior_context = _render_prior_round_context_section(round_num)
        return (
            "Role: code-writer worker for PM loop.\n"
            f"Current task_id: {task_id}, round: {round_num}.\n"
            f"Execute the contract below and only finish after writing {_display_path(WORK_REPORT)}.\n\n"
            "=== BEGIN docs/roles/code-writer.md ===\n"
            f"{role_text}\n"
            "=== END docs/roles/code-writer.md ===\n\n"
            f"=== FIX LIST (round {round_num}) ===\n"
            f"fixes:\n{fix_list_section}\n\n"
            f"{prior_context if prior_context else ''}"
        )

    agents_text = _read_text_with_default(
        ROOT / "AGENTS.md",
        "agents_md_default.txt",
    )
    role_text = _read_text_with_default(
        ROOT / "docs" / "roles" / "code-writer.md",
        "code_writer_md_default.txt",
    )
    orchestrator_path = ROOT / "src" / "loop_kit" / "orchestrator.py"
    task_card = _read_json_if_exists(TASK_CARD)
    task_card_section = _render_task_card_section(task_card if isinstance(task_card, dict) else {})
    prior_context_section = _render_prior_round_context_section(round_num)
    context = {
        "task_id": task_id,
        "round_num": str(round_num),
        "agents_md": agents_text,
        "role_md": role_text,
        "orchestrator_path": _display_path(orchestrator_path),
        "function_index": _function_index(orchestrator_path),
        "task_card_section": task_card_section,
        "prior_context_section": (f"\n{prior_context_section}" if prior_context_section else ""),
        "work_report_path": _display_path(WORK_REPORT),
    }
    return _render_prompt_template(
        template_path=_worker_prompt_template_path(),
        context=context,
    )


def _reviewer_prompt(task_id: str, round_num: int) -> str:
    role_text = _read_text_with_default(
        ROOT / "docs" / "roles" / "reviewer.md",
        "reviewer_md_default.txt",
    )
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
STATE_IDLE = "idle"
STATE_AWAITING_WORK = "awaiting_work"
STATE_AWAITING_REVIEW = "awaiting_review"
STATE_DONE = "done"


def _load_state() -> dict:
    default_state = {"state": STATE_IDLE, "round": 0, "task_id": None}
    if not STATE_FILE.exists():
        return default_state.copy()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _log(f"Warning: state.json is corrupted: {e}. Using fresh default state.")
        return default_state.copy()
    except OSError as e:
        _log(f"Warning: unable to read state.json: {e}. Using fresh default state.")
        return default_state.copy()
    if not isinstance(data, dict):
        _log("Warning: state.json root must be a JSON object. Using fresh default state.")
        return default_state.copy()
    return data


def _atomic_write_json(path: Path, data: object) -> None:
    """Write *data* as JSON to *path* atomically (write-then-rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _save_state(state: dict) -> None:
    _atomic_write_json(STATE_FILE, state)


# ── git helpers ─────────────────────────────────────────────────────
def _git(*args: str, timeout: float | None = DEFAULT_GIT_TIMEOUT_SEC) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_value = exc.timeout if exc.timeout is not None else timeout
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout_value}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _is_valid_ref(ref: str) -> bool:
    """Check that *ref* is a valid git rev (no argument injection)."""
    try:
        _git("rev-parse", "--verify", ref)
        return True
    except RuntimeError:
        return False


def _current_sha() -> str:
    return _git("rev-parse", "HEAD")


def _diff(base: str, head: str) -> str:
    return _git("diff", f"{base}..{head}")


def _log_oneline(base: str, head: str) -> str:
    return _git("log", "--oneline", f"{base}..{head}")


def _is_git_repo_root(path: Path) -> bool:
    return (path / ".git").exists() or (path / ".git").is_file()


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
    sys.exit(EXIT_DIRTY_WORKTREE)


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
    for field_name, typ in required_types.items():
        if field_name not in work:
            return f"work_report.json missing required field '{field_name}'"
        value = work[field_name]
        if typ is int:
            if type(value) is not int:
                return f"work_report field '{field_name}' must be int, got {type(value).__name__}"
        elif not isinstance(value, typ):
            return f"work_report field '{field_name}' must be {typ.__name__}, got {type(value).__name__}"
        if typ is str and not value.strip():
            return f"work_report field '{field_name}' must be non-empty"

    for list_field in ("files_changed", "tests"):
        if list_field in work and not isinstance(work[list_field], list):
            return f"work_report field '{list_field}' must be a list, got {type(work[list_field]).__name__}"

    if work["task_id"] != expected_task_id:
        return f"work_report field 'task_id' mismatch: expected {expected_task_id!r}, got {work['task_id']!r}"
    if work["round"] != expected_round:
        return f"work_report field 'round' mismatch: expected {expected_round}, got {work['round']!r}"
    return None


def _validate_review_report(
    review: dict,
    *,
    expected_task_id: str,
    expected_round: int,
) -> str | None:
    required_types: dict[str, type] = {
        "task_id": str,
        "round": int,
        "decision": str,
    }
    for field_name, typ in required_types.items():
        if field_name not in review:
            return f"review_report.json missing required field '{field_name}'"
        value = review[field_name]
        if typ is int:
            if type(value) is not int:
                return f"review_report field '{field_name}' must be int, got {type(value).__name__}"
        elif not isinstance(value, typ):
            return f"review_report field '{field_name}' must be {typ.__name__}, got {type(value).__name__}"
        if typ is str and not value.strip():
            return f"review_report field '{field_name}' must be non-empty"

    if review["decision"] not in {"approve", "changes_required"}:
        return (
            "review_report field 'decision' must be one of "
            "{'approve', 'changes_required'}, "
            f"got {review['decision']!r}"
        )
    if review["task_id"] != expected_task_id:
        return f"review_report field 'task_id' mismatch: expected {expected_task_id!r}, got {review['task_id']!r}"
    if review["round"] != expected_round:
        return f"review_report field 'round' mismatch: expected {expected_round}, got {review['round']!r}"
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
        print(f"\n  >>> Tell the {'Worker' if 'work' in path.name else 'Reviewer'} to process their input file. <<<\n")
    elapsed = 0
    last_ignored_signature: tuple[int, int] | None = None
    while True:
        if expected_role is not None:
            alive, reason = _role_is_alive(expected_role, heartbeat_ttl_sec)
            if not alive:
                _log(f"Stopping wait: {reason}")
                return None
        if path.exists():
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            data = _read_json_if_exists(path)
            if isinstance(data, dict):
                task_id = data.get("task_id")
                report_has_round = "round" in data
                round_num = data.get("round")
                if expected_task_id is not None and task_id != expected_task_id:
                    last_ignored_signature = signature
                elif expected_round is not None and report_has_round and round_num != expected_round:
                    if signature != last_ignored_signature:
                        _log(f"Ignoring stale {path.name}: expected round={expected_round}, got {round_num!r}")
                        last_ignored_signature = signature
                else:
                    _log(f"Found {path.name}")
                    return data
        if timeout_sec and elapsed >= timeout_sec:
            _log(f"Timeout ({timeout_sec}s) waiting for {path.name}")
            return None
        # Absolute safety cap: even with timeout_sec=0 (unlimited),
        # bail after 24 hours to prevent runaway processes.
        if elapsed >= _WAIT_SAFETY_CAP_SEC:
            _log(f"Safety cap (24h) reached waiting for {path.name}")
            return None
        time.sleep(POLL_INTERVAL_SEC)
        elapsed += POLL_INTERVAL_SEC


def _fail_with_state(state: dict, outcome: str, message: str, exit_code: int = EXIT_GENERAL_ERROR) -> None:
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
        example.write_text(
            json.dumps(
                {
                    "task_id": "T-001",
                    "goal": "<one-sentence goal>",
                    "in_scope": ["<file or module>"],
                    "out_of_scope": [],
                    "acceptance_criteria": ["<measurable criterion>"],
                    "constraints": [],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
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
    src = (archive_dir / restore_name).resolve()
    if not src.is_relative_to(archive_dir.resolve()):
        print("Error: restore path escapes archive directory", file=sys.stderr)
        sys.exit(EXIT_GENERAL_ERROR)
    if not src.exists():
        print(
            f"Error: archive file not found for task_id={task_id}: {src}",
            file=sys.stderr,
        )
        sys.exit(EXIT_GENERAL_ERROR)
    target_name = _restore_target_name_from_archive(src.stem)
    dest = LOOP_DIR / target_name
    shutil.copy2(src, dest)
    print(f"Restored {src.name} -> {dest}")


# ── extract-diff ────────────────────────────────────────────────────
def cmd_extract_diff(base: str, head: str) -> None:
    for ref in (base, head):
        if not _is_valid_ref(ref):
            print(f"Error: invalid git ref: {ref!r}", file=sys.stderr)
            sys.exit(EXIT_GENERAL_ERROR)
    print(_diff(base, head))


def cmd_heartbeat(role: str, interval: int) -> None:
    role = role.lower().strip()
    if role not in {"worker", "reviewer"}:
        print(f"Error: invalid role: {role}", file=sys.stderr)
        sys.exit(EXIT_GENERAL_ERROR)
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
            hb.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            time.sleep(max(1, interval))
    except KeyboardInterrupt:
        _log(f"Heartbeat stopped for role={role}")
        print("\n  Heartbeat stopped.")
        sys.exit(EXIT_OK)


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
        sys.exit(EXIT_GENERAL_ERROR)
    try:
        task_card = json.loads(tp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: task card at {tp} contains invalid JSON: {e}", file=sys.stderr)
        sys.exit(EXIT_GENERAL_ERROR)
    except OSError as e:
        print(f"Error: unable to read task card at {tp}: {e}", file=sys.stderr)
        sys.exit(EXIT_GENERAL_ERROR)
    if not isinstance(task_card, dict):
        print(f"Error: task card must be a JSON object: {tp}", file=sys.stderr)
        sys.exit(EXIT_GENERAL_ERROR)
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
    config: RunConfig,
    round_num: int,
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
        str(config.timeout),
        "--heartbeat-ttl",
        str(config.heartbeat_ttl),
        "--dispatch-backend",
        config.dispatch_backend,
        "--worker-backend",
        config.worker_backend,
        "--reviewer-backend",
        config.reviewer_backend,
        "--dispatch-timeout",
        str(config.dispatch_timeout),
        "--dispatch-retries",
        str(config.dispatch_retries),
        "--dispatch-retry-base-sec",
        str(config.dispatch_retry_base_sec),
        "--artifact-timeout",
        str(config.artifact_timeout),
    ]
    if config.require_heartbeat:
        cmd.append("--require-heartbeat")
    if config.auto_dispatch:
        cmd.append("--auto-dispatch")
    if config.allow_dirty:
        cmd.append("--allow-dirty")
    if config.verbose:
        cmd.append("--verbose")
    return cmd


def _print_round_header(round_num: int, role: str) -> None:
    title = role.capitalize()
    print(f"\n{'=' * 60}")
    print(f"  ROUND {round_num}  —  Awaiting {title}")
    print(f"{'=' * 60}")
    if role == "worker":
        print(f"  Task card: {TASK_CARD}")
        if round_num == 1:
            print("  Send task_card.json to Worker.")
        else:
            print("  Send fix_list.json to Worker.")
    elif role == "reviewer":
        print(f"  Review request: {REVIEW_REQ}")


def _auto_dispatch_role(
    role: str,
    prompt: str,
    config: RunConfig,
    task_id: str,
    round_num: int,
    artifact_path: Path,
) -> dict | None:
    if not config.auto_dispatch:
        return None
    backend = config.worker_backend if role == "worker" else config.reviewer_backend
    return _dispatch_with_artifact_fallback(
        role=role,
        dispatch_call=lambda: _run_auto_dispatch(
            role=role,
            backend=backend,
            prompt=prompt,
            timeout_sec=config.dispatch_timeout,
            verbose=config.verbose,
            dispatch_retries=config.dispatch_retries,
            dispatch_retry_base_sec=config.dispatch_retry_base_sec,
        ),
        artifact_path=artifact_path,
        task_id=task_id,
        round_num=round_num,
        timeout_sec=config.artifact_timeout,
    )


def _wait_for_role_result(
    role: str,
    artifact_path: Path,
    config: RunConfig,
    task_id: str,
    round_num: int,
) -> dict | None:
    return _wait_for_file(
        artifact_path,
        f"{role.capitalize()} result",
        timeout_sec=config.timeout,
        expected_task_id=task_id,
        expected_round=round_num,
        expected_role=role if config.require_heartbeat else None,
        heartbeat_ttl_sec=config.heartbeat_ttl,
        show_manual_hint=not config.auto_dispatch,
    )


def _print_blocking_issues(items: list[object]) -> None:
    print(f"  Blocking issues: {len(items)}")
    for issue in items:
        print(f"    - [{issue.get('severity', '?')}] {issue.get('file', '')}: {issue.get('reason', '')}")


def _run_single_round(
    *,
    config: RunConfig,
    round_num: int,
    single_round: bool,
) -> None:
    _ = single_round
    task_card, task_id_from_card = _sync_task_card_to_bus(config.task_path, round_num=round_num)

    state = _load_state()
    state_task_id = state.get("task_id")
    state_base_sha = state.get("base_sha")

    def _save_single_round_state() -> None:
        _archive_state_for_round(task_id_from_card, round_num)
        _save_state(state)

    def _fail_single_round(outcome: str, message: str, exit_code: int = EXIT_VALIDATION_ERROR) -> None:
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
                exit_code=EXIT_VALIDATION_ERROR,
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
                f"task_id mismatch between state.json and task card: state={state_task_id!r} task={task_id_from_card!r}"
            ),
            exit_code=EXIT_VALIDATION_ERROR,
        )
        return

    task_id = str(state_task_id)
    base_sha = str(state_base_sha)
    _set_feed_task_id(task_id)

    _log(f"Loaded task card: {task_id}")
    _log(f"Goal: {task_card.get('goal', '<no goal>')}")
    _log(f"Single-round state contract: task_id={task_id} base_sha={base_sha}")

    _clean_stale_state(state, "outcome", "failed_at", "error")
    if not isinstance(state.get("round_details"), list):
        state["round_details"] = []
    state["started_at"] = _ts()
    state["round"] = round_num
    state["state"] = STATE_AWAITING_WORK
    _save_single_round_state()

    worker_prompt = _worker_prompt(task_id, round_num)
    _prepare_bus_file(WORK_REPORT, task_id, round_num, "work_report")
    _prepare_bus_file(REVIEW_REPORT, task_id, round_num, "review_report")

    _print_round_header(round_num, "worker")

    work: dict | None = None
    try:
        work = _auto_dispatch_role(
            role="worker",
            prompt=worker_prompt,
            config=config,
            task_id=task_id,
            round_num=round_num,
            artifact_path=WORK_REPORT,
        )
    except RuntimeError as e:
        _fail_single_round(
            outcome="worker_dispatch_failed",
            message=str(e),
            exit_code=EXIT_VALIDATION_ERROR,
        )
        return

    if work is None:
        work = _wait_for_role_result(
            role="worker",
            artifact_path=WORK_REPORT,
            config=config,
            task_id=task_id,
            round_num=round_num,
        )
    if work is None:
        if config.require_heartbeat:
            _log("Worker unavailable or timed out. Aborting.")
            print("\n  Worker unavailable or timed out. Check .loop/runtime and logs.")
        else:
            _log("Worker timed out. Aborting.")
            print("\n  Worker did not respond in time. Check .loop/logs/ for details.")
        state["state"] = STATE_DONE
        state["outcome"] = "worker_timeout"
        state["error"] = "Worker timed out"
        _save_single_round_state()
        sys.exit(EXIT_TIMEOUT)

    report_error = _validate_work_report(
        work,
        expected_task_id=task_id,
        expected_round=round_num,
    )
    if report_error:
        _fail_single_round(
            outcome="invalid_work_report",
            message=report_error,
            exit_code=EXIT_VALIDATION_ERROR,
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
            exit_code=EXIT_VALIDATION_ERROR,
        )
        return

    try:
        diff = _diff(base_sha, head_sha)
        commits = _log_oneline(base_sha, head_sha)
    except RuntimeError as e:
        _fail_single_round(
            outcome="git_compare_failed",
            message=f"Failed to compare commits for base={base_sha} head={head_sha}: {e}",
        )
        return

    _log(f"Worker done. head_sha={head_sha}")
    print(f"  Worker completed: {head_sha[:8]}")
    print(f"  Files changed: {', '.join(work.get('files_changed', []))}")

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
    _prepare_bus_file(REVIEW_REPORT, task_id, round_num, "review_report")

    state["state"] = STATE_AWAITING_REVIEW
    state["head_sha"] = head_sha
    _save_single_round_state()

    _print_round_header(round_num, "reviewer")

    review: dict | None = None
    try:
        review = _auto_dispatch_role(
            role="reviewer",
            prompt=_reviewer_prompt(task_id, round_num),
            config=config,
            task_id=task_id,
            round_num=round_num,
            artifact_path=REVIEW_REPORT,
        )
    except RuntimeError as e:
        _fail_single_round(
            outcome="reviewer_dispatch_failed",
            message=str(e),
            exit_code=EXIT_VALIDATION_ERROR,
        )
        return

    if review is None:
        review = _wait_for_role_result(
            role="reviewer",
            artifact_path=REVIEW_REPORT,
            config=config,
            task_id=task_id,
            round_num=round_num,
        )
    if review is None:
        if config.require_heartbeat:
            _log("Reviewer unavailable or timed out. Aborting.")
        else:
            _log("Reviewer timed out. Aborting.")
        state["state"] = STATE_DONE
        state["outcome"] = "reviewer_timeout"
        state["error"] = "Reviewer timed out"
        _save_single_round_state()
        sys.exit(EXIT_TIMEOUT)

    review_error = _validate_review_report(
        review,
        expected_task_id=task_id,
        expected_round=round_num,
    )
    if review_error:
        _fail_single_round(
            outcome="invalid_review_report",
            message=review_error,
            exit_code=EXIT_VALIDATION_ERROR,
        )
        return

    decision = str(review["decision"])
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
        item
        for item in state.get("round_details", [])
        if not (isinstance(item, dict) and item.get("round") == round_num)
    ]
    round_details.append(round_detail)
    state["round_details"] = round_details

    if decision == "approve":
        state["state"] = STATE_DONE
        state["outcome"] = "approved"
        _save_single_round_state()
        print(f"\n{'=' * 60}")
        print(f"  APPROVED at round {round_num}")
        print(f"  base: {base_sha[:8]}  head: {head_sha[:8]}")
        print(f"{'=' * 60}")

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

    raw_blocking = review.get("blocking_issues", [])
    blocking = raw_blocking if isinstance(raw_blocking, list) else []
    blocking_items = [item for item in blocking if isinstance(item, dict)]
    fix_list = {
        "task_id": task_id,
        "round": round_num + 1,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "fixes": blocking_items,
        "prior_round_notes": work.get("notes", ""),
        "prior_review_non_blocking": review.get("non_blocking_suggestions", []),
    }
    _archive_bus_file(FIX_LIST, task_id, round_num, "fix_list")
    FIX_LIST.write_text(
        json.dumps(fix_list, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _prepare_bus_file(WORK_REPORT, task_id, round_num, "work_report")

    _print_blocking_issues(blocking_items)
    print(f"  Fix list written to {FIX_LIST}")

    state["state"] = STATE_AWAITING_WORK
    state["round"] = round_num + 1
    _save_single_round_state()


def _run_multi_round_via_subprocess(
    *,
    config: RunConfig,
    worktree_checked: bool = False,
    resume_from_state: dict | None = None,
) -> None:
    if not worktree_checked:
        _enforce_clean_worktree_or_exit(allow_dirty=config.allow_dirty)

    start_round = 1
    task_id = ""
    base_sha = ""
    if resume_from_state is None:
        task_card, task_id = _sync_task_card_to_bus(config.task_path, round_num=1)
        _set_feed_task_id(task_id)

        _log(f"Loaded task card: {task_id}")
        _log(f"Goal: {task_card.get('goal', '<no goal>')}")

        base_sha = _current_sha()
        _log(f"Base SHA: {base_sha}")

        state = _load_state()
        _clean_stale_state(state, "outcome", "failed_at", "error", "head_sha", "round_details")
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
                    "state.json is missing required resume contract (task_id/base_sha/round). Re-run without --resume."
                ),
                exit_code=EXIT_VALIDATION_ERROR,
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
                exit_code=EXIT_VALIDATION_ERROR,
            )
            return
        task_id = state_task_id
        base_sha = state_base_sha
        start_round = state_round
        _set_feed_task_id(task_id)
        _log(f"Resuming task: {task_id}")
        _log(f"Resume contract: base_sha={base_sha} round={start_round}")
        _clean_stale_state(state, "outcome", "failed_at", "error", "head_sha")
        if not isinstance(state.get("round_details"), list):
            state["round_details"] = []
        state["state"] = STATE_AWAITING_WORK
        state["round"] = start_round
        state["started_at"] = _ts()
        _save_state(state)

    _prepare_bus_file(WORK_REPORT, task_id, start_round, "work_report")
    _prepare_bus_file(REVIEW_REPORT, task_id, start_round, "review_report")
    for role in ("worker", "reviewer"):
        _dispatch_log_path(role).unlink(missing_ok=True)

    last_decision = "changes_required"
    interrupted = False
    current_proc: subprocess.Popen[str] | None = None

    def _outer_sigint_handler(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        if current_proc is not None and current_proc.poll() is None:
            _log(f"SIGINT received during round {round_num}; terminating subprocess")
            with contextlib.suppress(OSError):
                current_proc.terminate()

    old_sigint = signal.signal(signal.SIGINT, _outer_sigint_handler)

    try:
        for round_num in range(start_round, config.max_rounds + 1):
            if interrupted:
                break

            print(f"\n{'=' * 60}")
            print(f"  ROUND {round_num}/{config.max_rounds}  —  Single-Round Subprocess")
            print(f"{'=' * 60}")
            _archive_bus_file(STATE_FILE, task_id, round_num, "state")

            cmd = _single_round_subprocess_cmd(
                config=config,
                round_num=round_num,
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
            current_proc = proc
            stdout, stderr, returncode = _collect_streamed_text_output(
                proc,
                stdout_line_callback=lambda raw_line: print(raw_line, end="", flush=True),
            )
            current_proc = None

            if interrupted:
                _log(f"Round {round_num} subprocess terminated by SIGINT")
                break

            result = _completed_proc(
                cmd,
                returncode,
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
                    exit_code=EXIT_VALIDATION_ERROR,
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
                    exit_code=EXIT_VALIDATION_ERROR,
                )
                return

            if state.get("state") == STATE_DONE and state.get("outcome") == "approved":
                _archive_task_summary(task_id)
                _log(f"Task approved via state contract at round={round_num}")
                return

            if state.get("state") == STATE_AWAITING_WORK and state.get("round") == round_num + 1:
                last_decision = "changes_required"
                if (
                    isinstance(fix_list, dict)
                    and fix_list.get("task_id") == task_id
                    and fix_list.get("round") == round_num + 1
                ):
                    blocking = fix_list.get("fixes", [])
                    _print_blocking_issues(blocking)
                else:
                    _log(
                        "State indicates changes_required, but fix_list.json is missing/stale; "
                        "continuing based on state.json contract."
                    )
                if isinstance(review, dict):
                    decision = review.get("decision")
                    if decision not in (None, "changes_required"):
                        _log(f"Ignoring stale review_report decision={decision!r}; state.json is authoritative.")
                continue

            _fail_with_state(
                state,
                outcome="invalid_state_transition",
                message=(
                    "single-round subprocess exited 0 but did not produce a valid state transition: "
                    f"state={state.get('state')!r} outcome={state.get('outcome')!r} round={state.get('round')!r}"
                ),
                exit_code=EXIT_VALIDATION_ERROR,
            )
            return
    finally:
        signal.signal(signal.SIGINT, old_sigint)

    if interrupted:
        _fail_with_state(
            _load_state(),
            outcome="interrupted",
            message="User interrupted (SIGINT)",
            exit_code=EXIT_INTERRUPTED,
        )

    state = _load_state()
    state["state"] = STATE_DONE
    state["outcome"] = "max_rounds_exhausted"
    _save_state(state)
    print(f"\n  MAX ROUNDS ({config.max_rounds}) reached without approval.")
    print(f"  Last review decision: {last_decision}")
    print("  PM should re-evaluate task scope or split the task.")
    sys.exit(EXIT_GENERAL_ERROR)


def cmd_run(
    config: RunConfig,
    single_round: bool,
    round_num: int | None,
    resume: bool = False,
) -> None:
    lock: _LoopLock | None = None
    # Single-round subprocesses are spawned by the parent loop which already
    # holds the lock — skip lock acquisition to avoid self-deadlock.
    if not single_round:
        try:
            lock = _acquire_run_lock()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(EXIT_LOCK_FAILURE)
    try:
        # Single-round subprocesses are spawned by the parent loop which already
        # validated the worktree — skip redundant check to avoid duplicate warnings.
        if not single_round:
            _enforce_clean_worktree_or_exit(allow_dirty=config.allow_dirty)

        if resume and single_round:
            print("Error: --resume cannot be combined with --single-round", file=sys.stderr)
            sys.exit(EXIT_GENERAL_ERROR)

        if single_round:
            if round_num is None or round_num < 1:
                print("Error: --single-round requires --round N (N >= 1)", file=sys.stderr)
                sys.exit(EXIT_GENERAL_ERROR)
            _run_single_round(
                config=config,
                round_num=round_num,
                single_round=single_round,
            )
            return

        if round_num is not None:
            print("Error: --round is only valid together with --single-round", file=sys.stderr)
            sys.exit(EXIT_GENERAL_ERROR)

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
                sys.exit(EXIT_VALIDATION_ERROR)

        _run_multi_round_via_subprocess(
            config=config,
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
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {importlib.metadata.version('loop-kit')}",
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
    health_p.add_argument(
        "--ttl", type=int, default=DEFAULT_HEARTBEAT_TTL_SEC, help="Heartbeat freshness threshold in seconds"
    )

    hb_p = sub.add_parser("heartbeat", parents=[shared], help="Write role heartbeat continuously")
    hb_p.add_argument("--role", choices=["worker", "reviewer"], required=True)
    hb_p.add_argument("--interval", type=int, default=5, help="Heartbeat write interval in seconds")

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
    run_p.add_argument("--task", default=None, help="Path to task card JSON")
    run_p.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, help="Maximum review rounds (default: 3)")
    run_p.add_argument("--timeout", type=int, default=0, help="Per-phase timeout in seconds (0=unlimited)")
    run_p.add_argument(
        "--require-heartbeat", action="store_true", help="Require fresh worker/reviewer heartbeat while waiting"
    )
    run_p.add_argument(
        "--heartbeat-ttl", type=int, default=DEFAULT_HEARTBEAT_TTL_SEC, help="Heartbeat freshness threshold in seconds"
    )
    run_p.add_argument(
        "--auto-dispatch", action="store_true", help="Automatically invoke worker/reviewer backends each round"
    )
    run_p.add_argument(
        "--dispatch-backend",
        choices=[DISPATCH_BACKEND_NATIVE],
        default=DEFAULT_DISPATCH_BACKEND,
        help="Dispatch transport: native subprocess calls",
    )
    run_p.add_argument(
        "--worker-backend", default=DEFAULT_WORKER_BACKEND, help="Backend used for auto worker dispatch (native mode)"
    )
    run_p.add_argument(
        "--reviewer-backend",
        default=DEFAULT_REVIEWER_BACKEND,
        help="Backend used for auto reviewer dispatch (native mode)",
    )
    run_p.add_argument(
        "--dispatch-timeout",
        type=int,
        default=DEFAULT_DISPATCH_TIMEOUT_SEC,
        help="Per-dispatch timeout in seconds (default: 600, 0=unlimited)",
    )
    run_p.add_argument(
        "--dispatch-retries",
        type=int,
        default=DEFAULT_DISPATCH_RETRIES,
        help="Retry count for non-zero dispatch exits (default: 2)",
    )
    run_p.add_argument(
        "--dispatch-retry-base-sec",
        type=int,
        default=DEFAULT_DISPATCH_RETRY_BASE_SEC,
        help="Base retry backoff seconds (default: 5, max delay: 60)",
    )
    run_p.add_argument(
        "--artifact-timeout",
        type=int,
        default=DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        help="Post-dispatch artifact timeout in seconds (default: 90)",
    )
    run_p.add_argument("--single-round", action="store_true", help="Run exactly one round and exit")
    run_p.add_argument("--round", type=int, help="Round number for --single-round mode")
    run_p.add_argument("--allow-dirty", action="store_true", help="Allow run to start with dirty tracked git files")
    run_p.add_argument("--resume", action="store_true", help="Resume from .loop/state.json contract")
    run_p.add_argument("--verbose", action="store_true", help="Stream full backend stdout lines during auto-dispatch")

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
        config = RunConfig(
            task_path=args.task if args.task is not None else str(TASK_CARD),
            max_rounds=args.max_rounds,
            timeout=args.timeout,
            require_heartbeat=args.require_heartbeat,
            heartbeat_ttl=args.heartbeat_ttl,
            auto_dispatch=args.auto_dispatch,
            dispatch_backend=args.dispatch_backend,
            worker_backend=args.worker_backend,
            reviewer_backend=args.reviewer_backend,
            dispatch_timeout=args.dispatch_timeout,
            dispatch_retries=args.dispatch_retries,
            dispatch_retry_base_sec=args.dispatch_retry_base_sec,
            artifact_timeout=args.artifact_timeout,
            allow_dirty=args.allow_dirty,
            verbose=args.verbose,
        )
        cmd_run(
            config,
            single_round=args.single_round,
            round_num=args.round,
            resume=args.resume,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
