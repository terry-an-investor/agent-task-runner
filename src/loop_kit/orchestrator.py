"""
PM-driven review loop orchestrator.

Usage:
    loop init                                    # create .loop/ dirs
    loop index                                   # build offline module map
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
import ast
import contextlib
import hashlib
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, NotRequired, Required, TypedDict, cast

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class WorkReportTest(TypedDict):
    name: str
    result: str
    output: NotRequired[str]


class ReviewIssue(TypedDict):
    severity: str
    file: str
    reason: str
    id: NotRequired[str]
    required_change: NotRequired[str]
    category: NotRequired[str]
    confidence: NotRequired[int | float | str]


class WorkReport(TypedDict):
    task_id: str
    head_sha: str
    round: int
    files_changed: NotRequired[list[str]]
    tests: NotRequired[list[WorkReportTest]]
    notes: NotRequired[str]


class ReviewReport(TypedDict):
    task_id: str
    decision: str
    round: int
    blocking_issues: NotRequired[list[ReviewIssue]]
    non_blocking_suggestions: NotRequired[list[str]]


class ReviewRequest(TypedDict):
    task_id: str
    base_sha: str
    head_sha: str
    commits: str
    diff: str
    acceptance_criteria: list[str]
    constraints: list[str]
    round: int
    worker_notes: str
    worker_tests: list[WorkReportTest]


class TaskPacket(TypedDict):
    target_files: list[str]
    target_symbols: list[str]
    invariants: list[str]
    acceptance_checks: list[str]
    known_risks: list[str]
    commands_to_run: list[str]


class FixList(TypedDict, total=False):
    task_id: Required[str]
    round: Required[int]
    base_sha: Required[str]
    head_sha: Required[str]
    fixes: Required[list[ReviewIssue]]
    prior_round_notes: NotRequired[str]
    prior_review_non_blocking: NotRequired[list[str]]


class TaskCard(TypedDict, total=False):
    task_id: Required[str]
    goal: Required[str]
    in_scope: Required[list[str]]
    out_of_scope: Required[list[str]]
    acceptance_criteria: Required[list[str]]
    constraints: Required[list[str]]


# ── exception hierarchy ─────────────────────────────────────────────────────
class LoopKitError(Exception):
    """Base exception for all loop-kit errors."""

    pass


class StateError(LoopKitError):
    """Errors related to state management or state corruption."""

    pass


class DispatchError(LoopKitError):
    """Errors related to subprocess dispatch, timeouts, or backend failures."""

    pass


class ValidationError(LoopKitError):
    """Errors related to input validation, git state, or business rule violations."""

    pass


class ConfigError(LoopKitError):
    """Errors related to configuration loading or invalid config values."""

    pass


class DirtyWorktreeError(ValidationError):
    """Specific error for dirty git worktree."""

    pass


class StateError(LoopKitError):
    """Errors related to state management or state corruption."""

    pass


class DispatchError(LoopKitError):
    """Errors related to subprocess dispatch, timeouts, or backend failures."""

    pass


class ValidationError(LoopKitError):
    """Errors related to input validation, git state, or business rule violations."""

    pass


class ConfigError(LoopKitError):
    """Errors related to configuration loading or invalid config values."""

    pass


ROOT = Path.cwd()
LOOP_DIR = ROOT / ".loop"
LOGS_DIR = LOOP_DIR / "logs"
RUNTIME_DIR = LOOP_DIR / "runtime"
ARCHIVE_DIR = LOOP_DIR / "archive"
STATE_FILE = LOOP_DIR / "state.json"
_STATE_BACKUP = LOOP_DIR / ".state.json.bak"

DEFAULT_MAX_ROUNDS = 3
POLL_INTERVAL_SEC = 1
DEFAULT_HEARTBEAT_TTL_SEC = 30
DEFAULT_DISPATCH_TIMEOUT_SEC = 0
DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC = 90
DEFAULT_DISPATCH_RETRIES = 2
DEFAULT_DISPATCH_RETRY_BASE_SEC = 5
DEFAULT_MAX_SESSION_ROUNDS = 0
MAX_DISPATCH_RETRY_DELAY_SEC = 60
DEFAULT_GIT_TIMEOUT_SEC = 30
_STALE_STATE_KEYS = ("outcome", "failed_at", "error", "head_sha", "round_details")
FEED_DISPATCH_START = "dispatch_start"
FEED_DISPATCH_COMPLETE = "dispatch_complete"
FEED_DISPATCH_FAIL = "dispatch_fail"
FEED_DISPATCH_FIRST_ACTION = "dispatch_first_meaningful_action"
FEED_DISPATCH_ARTIFACT_WRITTEN = "dispatch_artifact_written"
FEED_DISPATCH_RESUME = "dispatch_resume"
FEED_ROUND_START = "round_start"
FEED_ROUND_COMPLETE = "round_complete"
FEED_REVIEW_VERDICT = "review_verdict"
FEED_HEARTBEAT = "heartbeat"
FEED_STATE_TRANSITION = "state_transition"
FEED_LOG = "log"
BACKEND_CODEX = "codex"
BACKEND_CLAUDE = "claude"
BACKEND_OPENCODE = "opencode"
DISPATCH_BACKEND_NATIVE = "native"
DEFAULT_WORKER_BACKEND = BACKEND_CODEX
DEFAULT_REVIEWER_BACKEND = BACKEND_CODEX
DEFAULT_DISPATCH_BACKEND = DISPATCH_BACKEND_NATIVE
DISPATCH_STREAM_POLL_SEC = 0.1
_WAIT_SAFETY_CAP_SEC = 86400  # 24h absolute cap in _wait_for_file
_SESSION_ROLES = ("worker", "reviewer")
EXIT_OK = 0
EXIT_GENERAL_ERROR = 1
EXIT_TIMEOUT = 2
EXIT_VALIDATION_ERROR = 3
EXIT_DIRTY_WORKTREE = 4
EXIT_LOCK_FAILURE = 5
EXIT_INTERRUPTED = 130
PATTERN_STALE_DAYS = 30
PATTERN_HIGH_CONFIDENCE = 0.7
_KNOWLEDGE_MAX_PATTERNS = 200
_KNOWLEDGE_MAX_PITFALL_LINES = 50
_FEED_TASK_ID: str | None = None
_FEED_ROUND: int | None = None
_LOGS_DIR_ENSURED = False
_LOGS_DIR_ENSURED_PATH: str | None = None
_stream_local = threading.local()
_AUTO_DISPATCH_HEARTBEATS: dict[str, tuple[threading.Event, threading.Thread]] = {}
_AUTO_DISPATCH_HEARTBEAT_LOCK = threading.Lock()
_AUTO_DISPATCH_HEARTBEAT_JOIN_TIMEOUT_SEC = 2.0


class DispatchTimeoutError(RuntimeError):
    """Dispatch command timed out before process exit."""


class PermanentDispatchError(RuntimeError):
    """Dispatch failed with a non-retriable error."""


@dataclass(frozen=True, slots=True)
class LoopPaths:
    root: Path
    dir: Path
    state: Path
    task_card: Path
    review_request: Path
    review_report: Path
    work_report: Path
    fix_list: Path
    summary: Path
    logs: Path
    archive: Path


# Backward compatibility bridge while path globals are migrated function-by-function.
_global_paths: LoopPaths | None = None


# ── file paths ──────────────────────────────────────────────────────
def _path(name: str) -> Path:
    return LOOP_DIR / name


TASK_CARD = _path("task_card.json")
FIX_LIST = _path("fix_list.json")
WORK_REPORT = _path("work_report.json")
REVIEW_REQ = _path("review_request.json")
REVIEW_REPORT = _path("review_report.json")
LOCK_FILE = _path("lock")
_SUMMARY_FILE = _path("summary.json")
_CONFIG_FILE = _path("config.json")
_TASKS_DIR = LOOP_DIR / "tasks"
TASK_PACKET = _path("task_packet.json")
_HANDOFF_DIR = LOOP_DIR / "handoff"
_CONTEXT_DIR = LOOP_DIR / "context"
_MODULE_MAP_FILE = _CONTEXT_DIR / "module_map.json"
_PROJECT_FACTS_FILE = _CONTEXT_DIR / "project_facts.md"
_PITFALLS_FILE = _CONTEXT_DIR / "pitfalls.md"
_PATTERNS_FILE = _CONTEXT_DIR / "patterns.jsonl"
_RESETTABLE_FILES = [
    LOCK_FILE,
    STATE_FILE,
    _STATE_BACKUP,
    _SUMMARY_FILE,
    WORK_REPORT,
    REVIEW_REPORT,
    REVIEW_REQ,
    FIX_LIST,
    TASK_CARD,
    TASK_PACKET,
]


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
    max_session_rounds: int = DEFAULT_MAX_SESSION_ROUNDS
    artifact_timeout: int = DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC
    allow_dirty: bool = False
    verbose: bool = False


def _resolve_loop_dir(loop_dir: str | Path) -> Path:
    candidate = Path(loop_dir)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def _build_loop_paths(loop_dir: Path) -> LoopPaths:
    resolved_dir = _resolve_loop_dir(loop_dir)
    return LoopPaths(
        root=ROOT,
        dir=resolved_dir,
        state=resolved_dir / "state.json",
        task_card=resolved_dir / "task_card.json",
        review_request=resolved_dir / "review_request.json",
        review_report=resolved_dir / "review_report.json",
        work_report=resolved_dir / "work_report.json",
        fix_list=resolved_dir / "fix_list.json",
        summary=resolved_dir / "summary.json",
        logs=resolved_dir / "logs",
        archive=resolved_dir / "archive",
    )


def _snapshot_global_paths() -> LoopPaths:
    return LoopPaths(
        root=ROOT,
        dir=LOOP_DIR,
        state=STATE_FILE,
        task_card=TASK_CARD,
        review_request=REVIEW_REQ,
        review_report=REVIEW_REPORT,
        work_report=WORK_REPORT,
        fix_list=FIX_LIST,
        summary=LOOP_DIR / "summary.json",
        logs=LOGS_DIR,
        archive=ARCHIVE_DIR,
    )


def _paths_match_globals(paths: LoopPaths) -> bool:
    current = _snapshot_global_paths()
    return (
        paths.root == current.root
        and paths.dir == current.dir
        and paths.state == current.state
        and paths.task_card == current.task_card
        and paths.review_request == current.review_request
        and paths.review_report == current.review_report
        and paths.work_report == current.work_report
        and paths.fix_list == current.fix_list
        and paths.summary == current.summary
        and paths.logs == current.logs
        and paths.archive == current.archive
    )


def _resolve_paths(paths: LoopPaths | None = None) -> LoopPaths:
    # TODO(paths): _render_fix_list_section/_render_task_packet_section/_resolve_task_path
    # still consume module globals; keep them synchronized via _global_paths during migration.
    if paths is not None:
        return paths
    if _global_paths is not None and _paths_match_globals(_global_paths):
        return _global_paths
    return _snapshot_global_paths()


def _apply_loop_paths(paths: LoopPaths) -> None:
    global LOOP_DIR
    global LOGS_DIR
    global RUNTIME_DIR
    global ARCHIVE_DIR
    global STATE_FILE
    global _STATE_BACKUP
    global TASK_CARD
    global FIX_LIST
    global WORK_REPORT
    global REVIEW_REQ
    global REVIEW_REPORT
    global LOCK_FILE
    global _SUMMARY_FILE
    global _CONFIG_FILE
    global _TASKS_DIR
    global _HANDOFF_DIR
    global _LOGS_DIR_ENSURED
    global _LOGS_DIR_ENSURED_PATH
    global TASK_PACKET
    global _CONTEXT_DIR
    global _MODULE_MAP_FILE
    global _PROJECT_FACTS_FILE
    global _PITFALLS_FILE
    global _PATTERNS_FILE
    global _FEED_ROUND

    LOOP_DIR = paths.dir
    LOGS_DIR = paths.logs
    RUNTIME_DIR = paths.dir / "runtime"
    ARCHIVE_DIR = paths.archive
    STATE_FILE = paths.state
    _STATE_BACKUP = paths.dir / ".state.json.bak"
    TASK_CARD = paths.task_card
    FIX_LIST = paths.fix_list
    WORK_REPORT = paths.work_report
    REVIEW_REQ = paths.review_request
    REVIEW_REPORT = paths.review_report
    LOCK_FILE = paths.dir / "lock"
    _SUMMARY_FILE = paths.summary
    _CONFIG_FILE = paths.dir / "config.json"
    _TASKS_DIR = paths.dir / "tasks"
    TASK_PACKET = paths.dir / "task_packet.json"
    _HANDOFF_DIR = paths.dir / "handoff"
    _CONTEXT_DIR = paths.dir / "context"
    _MODULE_MAP_FILE = _CONTEXT_DIR / "module_map.json"
    _PROJECT_FACTS_FILE = _CONTEXT_DIR / "project_facts.md"
    _PITFALLS_FILE = _CONTEXT_DIR / "pitfalls.md"
    _PATTERNS_FILE = _CONTEXT_DIR / "patterns.jsonl"
    _LOGS_DIR_ENSURED = False
    _LOGS_DIR_ENSURED_PATH = None
    _FEED_ROUND = None


def _configure_loop_paths(loop_dir: str | Path = ".loop") -> LoopPaths:
    global _global_paths
    paths = _build_loop_paths(Path(loop_dir))
    _global_paths = paths
    _apply_loop_paths(paths)
    return paths


def _loop_templates_dir(paths: LoopPaths | None = None) -> Path:
    resolved_paths = _resolve_paths(paths)
    return resolved_paths.dir / "templates"


def _worker_prompt_template_path(paths: LoopPaths | None = None) -> Path:
    return _loop_templates_dir(paths=paths) / "worker_prompt.txt"


def _reviewer_prompt_template_path(paths: LoopPaths | None = None) -> Path:
    return _loop_templates_dir(paths=paths) / "reviewer_prompt.txt"


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _task_archive_dir(task_id: str, paths: LoopPaths | None = None) -> Path:
    resolved_paths = _resolve_paths(paths)
    return resolved_paths.archive / task_id


def _task_handoff_dir(task_id: str, paths: LoopPaths | None = None) -> Path:
    _ = _resolve_paths(paths)
    return _HANDOFF_DIR / task_id


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


def _archive_task_summary(task_id: str, paths: LoopPaths | None = None) -> Path | None:
    resolved_paths = _resolve_paths(paths)
    summary_path = resolved_paths.summary
    if not summary_path.exists():
        return None
    archive_dir = _task_archive_dir(task_id, paths=resolved_paths)
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / "summary.json"
    shutil.copy2(summary_path, dest)
    return dest


def _archive_state_for_round(task_id: str, round_num: int, paths: LoopPaths | None = None) -> Path | None:
    """Capture the pre-round state snapshot once for this round."""
    resolved_paths = _resolve_paths(paths)
    dest = _task_archive_dir(task_id, paths=resolved_paths) / f"r{round_num}_state.json"
    if dest.exists():
        return dest
    return _archive_bus_file(resolved_paths.state, task_id, round_num, "state")


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


def _dispatch_log_path(role: str, paths: LoopPaths | None = None) -> Path:
    resolved_paths = _resolve_paths(paths)
    return resolved_paths.logs / f"{role}_dispatch.log"


def _feed_log_path(paths: LoopPaths | None = None) -> Path:
    resolved_paths = _resolve_paths(paths)
    return resolved_paths.logs / "feed.jsonl"


DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 3


# ── logging ─────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rotate_log_file(
    path: Path, max_bytes: int = DEFAULT_LOG_MAX_BYTES, backup_count: int = DEFAULT_LOG_BACKUP_COUNT
) -> None:
    if not path.exists():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < max_bytes:
        return
    for i in range(backup_count, 1, -1):
        src = path.with_suffix(f"{path.suffix}.{i - 1}")
        dest = path.with_suffix(f"{path.suffix}.{i}")
        if src.exists():
            if dest.exists() and i == backup_count:
                dest.unlink(missing_ok=True)
            src.replace(dest)
    path.replace(path.with_suffix(f"{path.suffix}.1"))


def _set_feed_task_id(task_id: str | None) -> None:
    global _FEED_TASK_ID
    _FEED_TASK_ID = task_id
    if task_id is None:
        _set_feed_round(None)


def _set_feed_round(round_num: int | None) -> None:
    global _FEED_ROUND
    _FEED_ROUND = round_num


def _feed_data(
    *,
    task_id: str | None = None,
    round_num: int | None = None,
    role: str | None = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": task_id if task_id is not None else _FEED_TASK_ID,
        "round": round_num if round_num is not None else _FEED_ROUND,
    }
    if role is not None:
        payload["role"] = role
    payload.update(extra)
    return payload


def _ensure_logs_dir(paths: LoopPaths | None = None) -> None:
    global _LOGS_DIR_ENSURED
    global _LOGS_DIR_ENSURED_PATH
    resolved_paths = _resolve_paths(paths)
    logs_dir = resolved_paths.logs
    current_logs_dir = _normalized_abs(logs_dir)
    if _LOGS_DIR_ENSURED and current_logs_dir == _LOGS_DIR_ENSURED_PATH and logs_dir.is_dir():
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    _LOGS_DIR_ENSURED = True
    _LOGS_DIR_ENSURED_PATH = current_logs_dir


def _feed_event(
    event: str,
    *,
    level: str = "info",
    data: dict | None = None,
    paths: LoopPaths | None = None,
) -> None:
    if _FEED_TASK_ID and data and data.get("task_id") not in (None, _FEED_TASK_ID):
        return
    payload_data = dict(data or {})
    if _FEED_TASK_ID and "task_id" not in payload_data:
        payload_data["task_id"] = _FEED_TASK_ID
    _ensure_logs_dir(paths=paths)
    feed_path = _feed_log_path(paths=paths)
    _rotate_log_file(feed_path)
    payload = {
        "ts": _ts(),
        "level": level,
        "event": event,
        "data": payload_data,
    }
    with open(feed_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _log(msg: str, paths: LoopPaths | None = None) -> None:
    ts = _ts()
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    resolved_paths = _resolve_paths(paths)
    _ensure_logs_dir(paths=resolved_paths)
    log_path = resolved_paths.logs / "orchestrator.log"
    _rotate_log_file(log_path)
    entry: dict[str, object] = {"ts": ts, "msg": msg}
    if _FEED_TASK_ID:
        entry["task_id"] = _FEED_TASK_ID
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _feed_event(FEED_LOG, data=_feed_data(role="orchestrator", message=msg), paths=resolved_paths)


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


def _auto_dispatch_heartbeat_payload(
    role: str,
    task_id: str | None,
    round_num: int | None,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "round_num": round_num,
        "role": role,
        "timestamp": _ts(),
    }


def _run_auto_dispatch_heartbeat_writer(
    role: str,
    stop_event: threading.Event,
    interval_sec: float,
    task_id: str | None,
    round_num: int | None,
) -> None:
    hb = _heartbeat_path(role)
    hb.parent.mkdir(parents=True, exist_ok=True)
    sleep_sec = max(1.0, float(interval_sec))
    while not stop_event.is_set():
        payload = _auto_dispatch_heartbeat_payload(
            role=role,
            task_id=task_id,
            round_num=round_num,
        )
        hb.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _feed_event(
            FEED_HEARTBEAT,
            data=_feed_data(
                task_id=task_id,
                round_num=round_num,
                role=role,
                source="auto_dispatch",
                timestamp=payload["timestamp"],
            ),
        )
        stop_event.wait(sleep_sec)


def _stop_auto_dispatch_heartbeat(role: str) -> None:
    with _AUTO_DISPATCH_HEARTBEAT_LOCK:
        active = _AUTO_DISPATCH_HEARTBEATS.pop(role, None)
    if active is None:
        return
    stop_event, thread = active
    stop_event.set()
    thread.join(timeout=_AUTO_DISPATCH_HEARTBEAT_JOIN_TIMEOUT_SEC)


def _start_auto_dispatch_heartbeat(
    role: str,
    *,
    heartbeat_ttl_sec: int,
    task_id: str | None,
    round_num: int | None,
) -> None:
    _stop_auto_dispatch_heartbeat(role)
    interval_sec = max(1.0, float(heartbeat_ttl_sec) / 2.0)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_auto_dispatch_heartbeat_writer,
        args=(role, stop_event, interval_sec, task_id, round_num),
        daemon=True,
        name=f"loop-kit-{role}-heartbeat",
    )
    with _AUTO_DISPATCH_HEARTBEAT_LOCK:
        _AUTO_DISPATCH_HEARTBEATS[role] = (stop_event, thread)
    try:
        thread.start()
    except Exception:
        with _AUTO_DISPATCH_HEARTBEAT_LOCK:
            current = _AUTO_DISPATCH_HEARTBEATS.get(role)
            if current is not None and current[0] is stop_event:
                _AUTO_DISPATCH_HEARTBEATS.pop(role, None)
        raise


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


def _extract_opencode_session_id(stdout: str) -> str | None:
    """Extract the session ID from step_start JSON events in opencode output."""
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "step_start":
            part = obj.get("part")
            if isinstance(part, dict):
                session_id = part.get("sessionID")
                if isinstance(session_id, str) and session_id.strip():
                    return session_id.strip()
    return None


def _flatten_text_payload(value: object, max_depth: int = 10) -> str:
    def _flatten(value: object, depth: int) -> str:
        if depth <= 0:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts = [_flatten(item, depth - 1) for item in value]
            return " ".join(part for part in parts if part).strip()
        if isinstance(value, dict):
            for key in ("text", "message", "content", "output_text", "value"):
                if key in value:
                    text = _flatten(value.get(key), depth - 1)
                    if text:
                        return text
        return ""

    return _flatten(value, max_depth)


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
    on_summary: Callable[[str], None] | None = None,
) -> None:
    read_state = getattr(_stream_local, "read_state", None)
    if read_state is None:
        read_state = {}
        _stream_local.read_state = read_state

    session_state = getattr(_stream_local, "session_state", None)
    if session_state is None:
        session_state = {}
        _stream_local.session_state = session_state

    state_key = f"{role}:{backend}"
    line = raw_line.rstrip("\r\n")
    summary = parse_event_fn(role, backend, line)
    if not summary:
        read_state.pop(state_key, None)
        return

    if summary in (
        f"[{role}] Step completed",
        f"[{role}] Turn completed",
        f"[{role}] Turn started",
    ):
        read_state.pop(state_key, None)
        return

    if summary.startswith(f"[{role}] Session:"):
        session_id = summary.split("Session:", 1)[1].strip()
        if session_state.get(role) == session_id:
            read_state.pop(state_key, None)
            return
        session_state[role] = session_id

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
        if on_summary is not None:
            on_summary(read_summary)
        read_state[state_key] = read_summary
        return

    is_tool_use = summary.startswith((f"[{role}] Running:", f"[{role}] Editing:", f"[{role}] Reading:"))
    if is_tool_use and read_state.get(state_key) == summary:
        return

    print(summary, flush=True)
    if on_summary is not None:
        on_summary(summary)
    if is_tool_use:
        read_state[state_key] = summary
    else:
        read_state.pop(state_key, None)


BackendBuildFn = Callable[..., tuple[list[str], str | None, str | None]]
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


_TOOL_READ_NAMES = frozenset({"read", "Read", "read_file"})
_TOOL_EDIT_NAMES = frozenset({"write", "Edit", "edit_file"})
_TOOL_WRITE_NAMES = frozenset({"Write", "write_file"})
_TOOL_BASH_NAMES = frozenset({"bash", "shell", "Bash"})
_TOOL_SEARCH_NAMES = frozenset({"Glob", "Grep"})
_TOOL_FETCH_NAMES = frozenset({"WebFetch", "WebSearch"})


def _tool_action_summary(role: str, tool_name: str, tool_input: dict | None) -> str | None:
    """Map a tool invocation to a human-readable stream summary.

    Shared by backends that expose tool-use events (claude, opencode).
    Returns ``None`` for unrecognized tools.
    """
    inp = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in _TOOL_READ_NAMES:
        fp = inp.get("filePath", "") or inp.get("file_path", "")
        return f"[{role}] Reading: {_short_filename(str(fp))}" if fp else f"[{role}] Reading file"
    if tool_name in _TOOL_EDIT_NAMES:
        fp = inp.get("filePath", "") or inp.get("file_path", "")
        return f"[{role}] Editing: {_short_filename(str(fp))}" if fp else f"[{role}] Editing files"
    if tool_name in _TOOL_WRITE_NAMES:
        fp = inp.get("filePath", "") or inp.get("file_path", "")
        return f"[{role}] Writing: {_short_filename(str(fp))}" if fp else f"[{role}] Writing file"
    if tool_name in _TOOL_BASH_NAMES:
        cmd_text = inp.get("command", "")
        return f"[{role}] Running: {_truncate_summary_text(str(cmd_text))}" if cmd_text else f"[{role}] Running command"
    if tool_name in _TOOL_SEARCH_NAMES:
        pattern = inp.get("pattern", "")
        return f"[{role}] Searching: {pattern}" if pattern else f"[{role}] Searching files"
    if tool_name in _TOOL_FETCH_NAMES:
        detail = inp.get("url", "") or inp.get("query", "")
        return f"[{role}] Fetching: {detail[:80]}" if detail else f"[{role}] Fetching"
    if tool_name:
        return f"[{role}] Tool: {tool_name}"
    return None


def _claude_parse_event(role: str, backend: str, line: str) -> str | None:
    _ = backend
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    if event_type == "system":
        if payload.get("subtype") == "init":
            session_id = payload.get("session_id", "")
            return f"[{role}] Session: {session_id}" if session_id else f"[{role}] Session started"
        return None
    if event_type == "assistant":
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        for block in message.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    return f"[{role}] Message: {_truncate_summary_text(text)}"
            if block.get("type") == "tool_use":
                summary = _tool_action_summary(role, block.get("name", ""), block.get("input"))
                if summary:
                    return summary
        return None
    if event_type == "result":
        return f"[{role}] Session completed"
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


def _build_codex_command(
    exe: str,
    prompt: str,
    resume_session_id: str | None = None,
) -> tuple[list[str], str | None, str | None]:
    cmd = [
        exe,
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(ROOT),
    ]
    sid = resume_session_id.strip() if isinstance(resume_session_id, str) and resume_session_id.strip() else None
    if sid:
        cmd.extend(["resume", sid])
    cmd.append(
        (
            "Execute the context provided via stdin. Follow the instructions"
            " embedded in it and only finish after the required output artifact"
            " is written."
        )
    )
    return (
        [
            *cmd,
        ],
        sid,
        prompt,
    )


def _build_claude_command(
    exe: str,
    prompt: str,
    resume_session_id: str | None = None,
) -> tuple[list[str], str | None, str | None]:
    sid = resume_session_id.strip() if isinstance(resume_session_id, str) and resume_session_id.strip() else ""
    if sid:
        # Resume existing session with --resume flag
        cmd = [
            exe,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--resume",
            sid,
        ]
    else:
        # New session: generate fresh UUID
        sid = str(uuid.uuid4())
        cmd = [
            exe,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--session-id",
            sid,
        ]
    return (cmd, sid, prompt)


def _resolve_backend_exe(backend: str) -> str:
    _, resolve_exe_fn, _ = _require_registered_backend(backend)
    return resolve_exe_fn(backend.strip().lower())


def _agent_command(
    backend: str,
    prompt: str,
    resume_session_id: str | None = None,
) -> tuple[list[str], str | None, str | None]:
    """Return (cmd, session_id, stdin_text).

    For codex >= 0.118.0 the prompt context is piped via stdin so the
    command line stays short.  The short CLI arg is a one-line instruction.
    """
    build_cmd_fn, _, _ = _require_registered_backend(backend)
    exe = _resolve_backend_exe(backend)
    backend_key = backend.strip().lower()
    if resume_session_id is not None and backend_key in {BACKEND_CODEX, BACKEND_CLAUDE, BACKEND_OPENCODE}:
        return build_cmd_fn(exe, prompt, resume_session_id)
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


def _build_opencode_command(
    exe: str,
    prompt: str,
    resume_session_id: str | None = None,
) -> tuple[list[str], str | None, str | None]:
    sid = resume_session_id.strip() if isinstance(resume_session_id, str) and resume_session_id.strip() else ""
    if not sid:
        sid = str(uuid.uuid4())
    cmd = [
        exe,
        "run",
        "--format",
        "json",
        "-s",
        sid,
        (
            "Execute the context provided via stdin.  Follow the instructions"
            " embedded in it and only finish after the required output artifact"
            " is written."
        ),
    ]
    return (cmd, sid, prompt)


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
        if not isinstance(state, dict) or state.get("status") == "error":
            return None
        summary = _tool_action_summary(role, tool_name, state.get("input"))
        return summary
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


def _dispatch_failure_hint(
    *,
    backend: str,
    stderr: str,
    timeout: bool = False,
    timeout_sec: int | None = None,
) -> str:
    hints: list[str] = []
    lowered = stderr.lower()
    effective_timeout_sec = DEFAULT_DISPATCH_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    if any(token in lowered for token in ("command not found", "not recognized")):
        hints.append(f"Backend {backend} not found. Run `{backend} --version` to verify installation.")
    if any(
        token in lowered
        for token in (
            "authentication",
            "auth token",
            "token expired",
            "auth failed",
            "api key",
            "unauthorized",
            "401",
            "403",
        )
    ):
        hints.append(f"Authentication failed for {backend}. Check your API key / token configuration.")
    if any(token in lowered for token in ("rate limit", "429", "quota")):
        hints.append(f"{backend} rate limit hit. Wait a moment or increase --dispatch-timeout.")
    if timeout or any(token in lowered for token in ("timeout", "timed out")):
        hints.append(
            f"Backend {backend} timed out. Try increasing --dispatch-timeout (current: {effective_timeout_sec}s)."
        )
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
    task_id: str | None = None,
    round_num: int | None = None,
) -> None:
    _write_dispatch_log(role, cmd, result, session_id)
    event_type = (
        FEED_DISPATCH_COMPLETE
        if timeout_sec is None and result.returncode == 0 and not interrupted
        else FEED_DISPATCH_FAIL
    )
    data = _feed_data(
        task_id=task_id,
        round_num=round_num,
        role=role,
        mode=DISPATCH_BACKEND_NATIVE,
        backend=backend,
        returncode=result.returncode,
        attempt=attempt,
        max_attempts=max_attempts,
    )
    if timeout_sec is not None:
        data["timeout_sec"] = timeout_sec
    if session_id is not None:
        data["session_id"] = session_id
    if stdout_len is not None:
        data["stdout_len"] = stdout_len
    if interrupted:
        data["interrupted"] = True
    _feed_event(
        event_type,
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
    summary_callback: Callable[[str], None] | None = None,
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
                on_summary=summary_callback,
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
        stdin_thread.join(timeout=5.0)
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
        stream_error = False
        try:
            for raw_line in proc.stdout:
                stdout_chunks.append(raw_line)
                if stdout_line_callback is not None:
                    stdout_line_callback(raw_line)
        except Exception:
            stream_error = True
            raise
        finally:
            _close_pipe(proc.stdout)
            if stream_error:
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    proc.wait(timeout=1)

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


def _is_invalid_resume_session_error(text: str) -> bool:
    lowered = text.lower()
    if "session" not in lowered and "thread" not in lowered:
        return False
    return any(
        token in lowered
        for token in (
            "invalid",
            "not found",
            "no rollout found",
            "unknown",
            "expired",
            "does not exist",
            "no such",
        )
    )


def _is_meaningful_dispatch_summary(role: str, summary: str) -> bool:
    prefixes = (
        f"[{role}] Running:",
        f"[{role}] Editing:",
        f"[{role}] Writing:",
        f"[{role}] Reading:",
        f"[{role}] Searching:",
        f"[{role}] Fetching:",
        f"[{role}] Message:",
        f"[{role}] Tool:",
    )
    return summary.startswith(prefixes)


def _run_auto_dispatch(
    role: str,
    backend: str,
    prompt: str,
    timeout_sec: int,
    *,
    verbose: bool = False,
    dispatch_retries: int = DEFAULT_DISPATCH_RETRIES,
    dispatch_retry_base_sec: int = DEFAULT_DISPATCH_RETRY_BASE_SEC,
    heartbeat_enabled: bool = False,
    heartbeat_ttl_sec: int = DEFAULT_HEARTBEAT_TTL_SEC,
    task_id: str | None = None,
    round_num: int | None = None,
    resume_session_id: str | None = None,
) -> str | None:
    parse_event_fn = _require_registered_parse_event(backend)
    retry_count = max(0, int(dispatch_retries))
    retry_base_sec = max(1, int(dispatch_retry_base_sec))
    max_attempts = retry_count + 1
    if isinstance(resume_session_id, str):
        active_resume_session_id = resume_session_id.strip() or None
    else:
        active_resume_session_id = None
    _log(f"Auto-dispatch start: role={role} backend={backend} retries={retry_count} retry_base_sec={retry_base_sec}")
    if heartbeat_enabled:
        _start_auto_dispatch_heartbeat(
            role,
            heartbeat_ttl_sec=heartbeat_ttl_sec,
            task_id=task_id,
            round_num=round_num,
        )
    attempt = 0
    try:
        while attempt < max_attempts:
            attempt += 1
            attempt_started_at = time.perf_counter()
            first_meaningful_action_ms: int | None = None

            def _on_summary(summary: str) -> None:
                nonlocal first_meaningful_action_ms
                if first_meaningful_action_ms is not None:
                    return
                if not _is_meaningful_dispatch_summary(role, summary):
                    return
                first_meaningful_action_ms = max(0, int((time.perf_counter() - attempt_started_at) * 1000))
                _feed_event(
                    FEED_DISPATCH_FIRST_ACTION,
                    data=_feed_data(
                        task_id=task_id,
                        round_num=round_num,
                        role=role,
                        backend=backend,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        latency_ms=first_meaningful_action_ms,
                        summary=summary,
                    ),
                )

            if active_resume_session_id is None:
                cmd, cmd_sid, stdin_text = _agent_command(backend, prompt)
            else:
                cmd, cmd_sid, stdin_text = _agent_command(
                    backend,
                    prompt,
                    resume_session_id=active_resume_session_id,
                )
            _feed_event(
                FEED_DISPATCH_START,
                data=_feed_data(
                    task_id=task_id,
                    round_num=round_num,
                    role=role,
                    mode=DISPATCH_BACKEND_NATIVE,
                    backend=backend,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    timeout_sec=timeout_sec,
                    resume_requested=active_resume_session_id is not None,
                ),
            )
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
                    summary_callback=_on_summary,
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
                    task_id=task_id,
                    round_num=round_num,
                )
                raise
            if first_meaningful_action_ms is None:
                _feed_event(
                    FEED_DISPATCH_FIRST_ACTION,
                    level="warning",
                    data=_feed_data(
                        task_id=task_id,
                        round_num=round_num,
                        role=role,
                        backend=backend,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        latency_ms=None,
                        status="not_observed",
                    ),
                )
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
                    task_id=task_id,
                    round_num=round_num,
                )
                raise DispatchTimeoutError(
                    f"{role} dispatch timeout after {timeout_sec}s (backend={backend})."
                    + _dispatch_failure_hint(
                        backend=backend,
                        stderr=stderr or "",
                        timeout=True,
                        timeout_sec=timeout_sec,
                    )
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
            elif backend == BACKEND_OPENCODE:
                parsed = _extract_opencode_session_id(result.stdout or "")
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
                task_id=task_id,
                round_num=round_num,
            )

            if result.returncode == 0:
                _log(f"Auto-dispatch done: role={role} backend={backend} attempts={attempt}")
                return session_id

            stdout_text = (result.stdout or "").strip()
            stderr_text = (result.stderr or "").strip()
            if active_resume_session_id and (
                _is_invalid_resume_session_error(stderr_text) or _is_invalid_resume_session_error(stdout_text)
            ):
                _log(f"{role} resume session is invalid for backend={backend}; falling back to a new session.")
                _feed_event(
                    FEED_DISPATCH_RESUME,
                    level="warning",
                    data=_feed_data(
                        task_id=task_id,
                        round_num=round_num,
                        role=role,
                        backend=backend,
                        status="fallback_invalid_resume",
                        attempt=attempt,
                        session_id=active_resume_session_id,
                    ),
                )
                active_resume_session_id = None
                max_attempts += 1
                continue
            if _is_permanent_dispatch_error(stderr_text):
                raise PermanentDispatchError(
                    f"{role} dispatch failed with permanent error (backend={backend}, rc={result.returncode}): "
                    f"{stderr_text} — permanent error, not retrying."
                    + _dispatch_failure_hint(
                        backend=backend,
                        stderr=stderr_text,
                        timeout_sec=timeout_sec,
                    )
                )
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"{role} dispatch failed (backend={backend}, rc={result.returncode}) "
                    f"after {attempt} attempts: {stderr_text}"
                    + _dispatch_failure_hint(
                        backend=backend,
                        stderr=stderr_text,
                        timeout_sec=timeout_sec,
                    )
                )
            retry_delay = min(MAX_DISPATCH_RETRY_DELAY_SEC, retry_base_sec * (2 ** (attempt - 1)))
            _log(
                f"{role} dispatch failed (backend={backend}, rc={result.returncode}) on attempt "
                f"{attempt}/{max_attempts}; retrying in {retry_delay}s"
            )
            time.sleep(retry_delay)
    finally:
        if heartbeat_enabled:
            _stop_auto_dispatch_heartbeat(role)


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
        if isinstance(data, dict) and data.get("task_id") == task_id and data.get("round") == round_num:
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


def _strip_list_prefix(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("- "):
        return stripped[2:].strip()
    if stripped.startswith("* "):
        return stripped[2:].strip()
    return stripped


def _source_version_from_file(path: Path) -> str:
    try:
        digest = hashlib.sha1(path.read_bytes()).hexdigest()
        return digest[:8]
    except OSError:
        try:
            fallback = str(path.stat().st_mtime_ns)
        except OSError:
            fallback = str(time.time_ns())
        return hashlib.sha1(fallback.encode("utf-8")).hexdigest()[:8]


def _load_markdown_knowledge_entries(path: Path, *, field_name: str) -> list[dict[str, str]]:
    text = _read_text_optional(path)
    if not text:
        return []
    source_version = _source_version_from_file(path)
    entries: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("<!--"):
            continue
        normalized = _strip_list_prefix(line)
        if normalized:
            entries.append({field_name: normalized, "source_version": source_version})
    return entries


def _load_project_facts() -> list[dict[str, str]]:
    return _load_markdown_knowledge_entries(_PROJECT_FACTS_FILE, field_name="fact")


def _load_pitfalls() -> list[dict[str, str]]:
    return _load_markdown_knowledge_entries(_PITFALLS_FILE, field_name="pitfall")


def _read_markdown_knowledge_lines(path: Path) -> list[str]:
    entries = _load_markdown_knowledge_entries(path, field_name="text")
    return [entry["text"] for entry in entries]


def _parse_utc_iso8601(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_utc_iso8601(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_confidence(value: object, *, default: float = 0.0) -> float:
    raw: float
    if isinstance(value, bool):
        raw = 1.0 if value else 0.0
    elif isinstance(value, int | float):
        raw = float(value)
    elif isinstance(value, str):
        try:
            raw = float(value.strip())
        except ValueError:
            raw = default
    else:
        raw = default
    return max(0.0, min(1.0, raw))


def _normalize_pattern_entry(
    entry: object,
    *,
    now_utc: datetime,
    source_version: str,
) -> tuple[dict | None, bool, bool]:
    if not isinstance(entry, dict):
        return None, False, False
    pattern = entry.get("pattern")
    category = entry.get("category")
    if not isinstance(pattern, str) or not pattern.strip():
        return None, False, False
    if not isinstance(category, str) or not category.strip():
        return None, False, False

    parsed_verified = _parse_utc_iso8601(entry.get("last_verified"))
    if parsed_verified is None:
        last_verified = "1970-01-01T00:00:00Z"
        stale = True
    else:
        last_verified = _to_utc_iso8601(parsed_verified)
        stale = now_utc - parsed_verified > timedelta(days=PATTERN_STALE_DAYS)

    confidence = _coerce_confidence(entry.get("confidence"), default=0.0)
    if stale:
        confidence = 0.0

    normalized = {
        "pattern": pattern.strip(),
        "category": category.strip(),
        "confidence": confidence,
        "last_verified": last_verified,
        "source_version": source_version,
    }
    changed = any(
        (
            entry.get("pattern") != normalized["pattern"],
            entry.get("category") != normalized["category"],
            entry.get("confidence") != normalized["confidence"],
            entry.get("last_verified") != normalized["last_verified"],
        )
    )
    return normalized, changed, stale


def _write_patterns_jsonl(entries: list[dict]) -> None:
    _PATTERNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps({k: v for k, v in item.items() if k != "source_version"}, ensure_ascii=False) + "\n"
        for item in entries
    )
    _PATTERNS_FILE.write_text(payload, encoding="utf-8")


def _load_patterns_with_governance(*, persist: bool = False) -> tuple[list[dict], int]:
    text = _read_text_optional(_PATTERNS_FILE)
    if not text:
        return [], 0
    source_version = _source_version_from_file(_PATTERNS_FILE)
    now_utc = datetime.now(UTC)
    deduped: dict[tuple[str, str], tuple[dict, bool]] = {}
    duplicate_count = 0
    changed = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            raw_entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        normalized, entry_changed, stale = _normalize_pattern_entry(
            raw_entry,
            now_utc=now_utc,
            source_version=source_version,
        )
        if normalized is None:
            continue
        changed = changed or entry_changed
        key = (normalized["category"], normalized["pattern"])
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = (normalized, stale)
            continue
        duplicate_count += 1
        existing_entry, existing_stale = existing
        if _coerce_confidence(normalized.get("confidence"), default=0.0) > _coerce_confidence(
            existing_entry.get("confidence"), default=0.0
        ):
            deduped[key] = (normalized, stale)
        else:
            deduped[key] = (existing_entry, existing_stale)

    entries = [entry for entry, _ in deduped.values()]
    stale_count = sum(1 for _, stale in deduped.values() if stale)
    if duplicate_count > 0:
        changed = True
    _feed_event(
        FEED_LOG,
        level="debug",
        data=_feed_data(
            role="orchestrator",
            message=f"Pattern deduplication: {duplicate_count} duplicates removed, {len(entries)} unique kept",
        ),
    )

    if persist and changed:
        _write_patterns_jsonl(entries)
        persisted_source_version = _source_version_from_file(_PATTERNS_FILE)
        for entry in entries:
            entry["source_version"] = persisted_source_version
    return entries, stale_count


def _format_pattern_prompt_line(entry: dict) -> str:
    confidence = _coerce_confidence(entry.get("confidence"), default=0.0)
    category = entry.get("category", "")
    pattern = entry.get("pattern", "")
    last_verified = entry.get("last_verified", "")
    return f"[{confidence:.2f}] ({category}) {pattern} (verified {last_verified})"


def _render_knowledge_section() -> str:
    project_fact_entries = _load_project_facts()
    pitfall_entries = _load_pitfalls()
    project_facts = [entry["fact"] for entry in project_fact_entries]
    active_pitfalls = [entry["pitfall"] for entry in pitfall_entries]
    patterns, _ = _load_patterns_with_governance(persist=False)
    high_conf_patterns = [
        _format_pattern_prompt_line(entry)
        for entry in patterns
        if _coerce_confidence(entry.get("confidence"), default=0.0) >= PATTERN_HIGH_CONFIDENCE
    ]
    if not project_facts and not active_pitfalls and not high_conf_patterns:
        return "- <none>"
    return (
        "project_facts:\n"
        f"{_as_prompt_list(project_facts)}\n\n"
        "active_pitfalls:\n"
        f"{_as_prompt_list(active_pitfalls)}\n\n"
        "high_confidence_patterns:\n"
        f"{_as_prompt_list(high_conf_patterns)}"
    )


_function_index_cache: tuple[tuple[int, float], str] | None = None


def _build_task_packet(task_card: TaskCard, round_num: int) -> TaskPacket:
    in_scope = task_card.get("in_scope", [])
    target_files: list[str] = []
    for item in in_scope:
        if not isinstance(item, str):
            continue
        matched = sorted(p.relative_to(ROOT).as_posix() for p in ROOT.glob(item) if p.is_file())
        if matched:
            target_files.extend(matched)
        else:
            resolved = (ROOT / item).resolve()
            if resolved.is_file():
                target_files.append(resolved.relative_to(ROOT).as_posix())

    target_symbols: list[str] = []
    for filepath in target_files:
        index_text = _function_index(ROOT / filepath)
        if index_text and index_text != "- <none>" and index_text != "- <unavailable>":
            for line in index_text.splitlines():
                stripped = line.strip()
                if stripped:
                    target_symbols.append(stripped)

    constraints = task_card.get("constraints", [])
    invariants: list[str] = [c for c in constraints if isinstance(c, str)]

    acceptance_criteria = task_card.get("acceptance_criteria", [])
    acceptance_checks: list[str] = [c for c in acceptance_criteria if isinstance(c, str)]

    known_risks: list[str] = [entry["pitfall"] for entry in _load_pitfalls()]

    if round_num > 1:
        fix_list_data = _read_json_if_exists(FIX_LIST)
        if isinstance(fix_list_data, dict):
            fix_list = cast(FixList, fix_list_data)
            fixes = fix_list.get("fixes", [])
            if isinstance(fixes, list):
                for issue in fixes:
                    if isinstance(issue, dict):
                        severity = issue.get("severity", "?")
                        file = issue.get("file", "")
                        reason = issue.get("reason", "")
                        known_risks.append(f"[{severity}] {file}: {reason}")

    commands_to_run = [
        "uv run --group dev pytest",
        "uv run python -m py_compile src/loop_kit/orchestrator.py",
    ]

    return {
        "target_files": target_files,
        "target_symbols": target_symbols,
        "invariants": invariants,
        "acceptance_checks": acceptance_checks,
        "known_risks": known_risks,
        "commands_to_run": commands_to_run,
    }


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

    result = "- <none>" if not entries else "\n".join(entries)
    _function_index_cache = (key, result)
    return result


def _render_task_card_section(task_card: TaskCard) -> str:
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


def _render_quickstart_context_section(task_card: TaskCard) -> str:
    return (
        "project_baseline:\n"
        "- Core owner: src/loop_kit/orchestrator.py (single-file orchestrator architecture)\n"
        "- Wrappers: src/loop_kit/cli.py, src/loop_kit/__main__.py, src/loop_kit/__init__.py\n"
        "- Primary tests: tests/test_orchestrator.py, tests/test_integration.py\n"
        "execution_constraints:\n"
        "- state.json is the single source of truth between outer and inner processes\n"
        "- JSON writes use UTF-8 with ensure_ascii=False and indent=2\n"
        "- Extend backends through register_backend() instead of dispatch rewrites\n"
        f"task_goal: {task_card.get('goal', '<none>')}\n"
        "task_constraints:\n"
        f"{_as_prompt_list(task_card.get('constraints'))}\n"
    )


def _handoff_round_from_filename(path: Path, role: str) -> int | None:
    prefix = f"{role}_r"
    suffix = ".json"
    name = path.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    token = name[len(prefix) : -len(suffix)]
    if not token.isdigit():
        return None
    return int(token)


def _latest_handoff_entry(
    task_id: str,
    role: str,
    *,
    before_round: int,
    paths: LoopPaths | None = None,
) -> dict | None:
    if before_round <= 1:
        return None
    handoff_dir = _task_handoff_dir(task_id, paths=paths)
    if not handoff_dir.is_dir():
        return None
    latest_round: int | None = None
    latest_data: dict | None = None
    for path in sorted(handoff_dir.glob(f"{role}_r*.json")):
        round_num = _handoff_round_from_filename(path, role)
        if round_num is None or round_num >= before_round:
            continue
        data = _read_json_if_exists(path)
        if not isinstance(data, dict):
            continue
        if data.get("task_id") != task_id:
            continue
        if data.get("role") != role:
            continue
        if latest_round is None or round_num > latest_round:
            latest_round = round_num
            latest_data = data
    return latest_data


def _render_handoff_context_section(task_id: str, round_num: int, paths: LoopPaths | None = None) -> str:
    records: list[tuple[str, dict]] = []
    for role in _SESSION_ROLES:
        data = _latest_handoff_entry(task_id, role, before_round=round_num, paths=paths)
        if isinstance(data, dict):
            records.append((role, data))
    if not records:
        return "- <none>"

    rendered: list[str] = []
    for role, data in records:
        rendered.extend(
            [
                f"role: {role}",
                f"round: {data.get('round', '<none>')}",
                "done:",
                _as_prompt_list(data.get("done")),
                "open_questions:",
                _as_prompt_list(data.get("open_questions")),
                "next_actions:",
                _as_prompt_list(data.get("next_actions")),
                "evidence:",
                _as_prompt_list(data.get("evidence")),
                "must_read_files:",
                _as_prompt_list(data.get("must_read_files")),
                "",
            ]
        )
    if rendered and rendered[-1] == "":
        rendered.pop()
    return "\n".join(rendered)


def _string_list(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text:
            result.append(text)
    return result


def _issue_file_list(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("file", "")).strip()
        if file_path and file_path not in result:
            result.append(file_path)
    return result


def _write_handoff_artifact(
    *,
    task_id: str,
    role: str,
    round_num: int,
    done: list[str],
    open_questions: list[str],
    next_actions: list[str],
    evidence: list[str],
    must_read_files: list[str],
    paths: LoopPaths | None = None,
) -> Path:
    handoff_dir = _task_handoff_dir(task_id, paths=paths)
    handoff_dir.mkdir(parents=True, exist_ok=True)
    target = handoff_dir / f"{role}_r{round_num}.json"
    payload = {
        "task_id": task_id,
        "role": role,
        "round": round_num,
        "created_at": _ts(),
        "done": done,
        "open_questions": open_questions,
        "next_actions": next_actions,
        "evidence": evidence,
        "must_read_files": must_read_files,
    }
    _atomic_write_json(target, payload)
    return target


def _persist_worker_handoff(
    *,
    task_id: str,
    round_num: int,
    work: WorkReport,
    paths: LoopPaths | None = None,
) -> None:
    tests_summary = _tests_summary(work.get("tests", []))
    notes = str(work.get("notes", "")).strip()
    head_sha = str(work.get("head_sha", "")).strip()
    files_changed = _string_list(work.get("files_changed"))
    done = [f"Worker produced work_report.json for round {round_num}."]
    if head_sha:
        done.append(f"Head commit: {head_sha}")
    if notes:
        done.append(f"Worker notes: {notes}")
    evidence = [
        f"tests_total={tests_summary['total']} pass={tests_summary['pass']} fail={tests_summary['fail']} other={tests_summary['other']}",
        f"files_changed_count={len(files_changed)}",
    ]
    if files_changed:
        evidence.append("files_changed=" + ", ".join(files_changed))
    _write_handoff_artifact(
        task_id=task_id,
        role="worker",
        round_num=round_num,
        done=done,
        open_questions=[],
        next_actions=["Reviewer validates review_request.json against acceptance criteria and constraints."],
        evidence=evidence,
        must_read_files=files_changed,
        paths=paths,
    )


def _persist_reviewer_handoff(
    *,
    task_id: str,
    round_num: int,
    review: ReviewReport,
    paths: LoopPaths | None = None,
) -> None:
    decision = str(review.get("decision", "")).strip() or "changes_required"
    blocking = review.get("blocking_issues", [])
    non_blocking = _string_list(review.get("non_blocking_suggestions"))
    done = [f"Reviewer decision for round {round_num}: {decision}."]
    evidence = [f"blocking_issues={len(blocking) if isinstance(blocking, list) else 0}"]
    if decision == "approve":
        next_actions = ["No further implementation changes required for this task."]
    else:
        next_actions = ["Worker must address all blocking issues in fix_list.json in the next round."]
    must_read_files = _issue_file_list(blocking)
    _write_handoff_artifact(
        task_id=task_id,
        role="reviewer",
        round_num=round_num,
        done=done,
        open_questions=non_blocking,
        next_actions=next_actions,
        evidence=evidence,
        must_read_files=must_read_files,
        paths=paths,
    )


def _render_prior_round_context_section(round_num: int) -> str | None:
    if round_num <= 1:
        return None
    work_data = _read_json_if_exists(WORK_REPORT)
    review_data = _read_json_if_exists(REVIEW_REPORT)
    if not isinstance(work_data, dict) or not isinstance(review_data, dict):
        return None
    work = cast(WorkReport, work_data)
    review = cast(ReviewReport, review_data)

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
    "=== QUICKSTART CONTEXT ===\n{quickstart_section}\n\n"
    "=== HANDOFF CONTEXT ===\n{handoff_section}\n\n"
    "=== KNOWLEDGE ===\n{knowledge_section}\n\n"
    "=== TASK PACKET ===\n{task_packet_section}\n\n"
    "{task_card_section}{prior_context_section}"
)


DEFAULT_REVIEWER_PROMPT_TEMPLATE = (
    "Role: reviewer for PM loop.\n"
    "Current task_id: {task_id}, round: {round_num}.\n"
    "Execute the contract below and only finish after writing {review_report_path}.\n\n"
    "=== HANDOFF CONTEXT ===\n{handoff_section}\n\n"
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
    _ = round_num
    fix_list_data = _read_json_if_exists(FIX_LIST)
    if not isinstance(fix_list_data, dict):
        return "- <none>"
    fix_list = cast(FixList, fix_list_data)
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


def _render_task_packet_section() -> str:
    packet_data = _read_json_if_exists(TASK_PACKET)
    if not isinstance(packet_data, dict):
        return "- <none>"
    packet = cast(TaskPacket, packet_data)
    lines = []
    target_files = packet.get("target_files", [])
    lines.append(f"target_files:\n{_as_prompt_list(target_files)}")
    target_symbols = packet.get("target_symbols", [])
    lines.append(f"target_symbols:\n{_as_prompt_list(target_symbols)}")
    invariants = packet.get("invariants", [])
    lines.append(f"invariants:\n{_as_prompt_list(invariants)}")
    acceptance_checks = packet.get("acceptance_checks", [])
    lines.append(f"acceptance_checks:\n{_as_prompt_list(acceptance_checks)}")
    known_risks = packet.get("known_risks", [])
    lines.append(f"known_risks:\n{_as_prompt_list(known_risks)}")
    commands_to_run = packet.get("commands_to_run", [])
    lines.append(f"commands_to_run:\n{_as_prompt_list(commands_to_run)}")
    return "\n\n".join(lines)


def _build_prompt(header: str, sections: list[tuple[str, str]]) -> str:
    parts = [header]
    for title, content in sections:
        if title:
            parts.append(f"{title}\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


def _join_prompt_sections(sections: list[tuple[str, str]]) -> str:
    parts = []
    for title, content in sections:
        parts.append(f"{title}\n{content}")
    return "\n\n".join(parts)


def _build_prompt_sections(task_id: str, round_num: int) -> list[tuple[str, str]]:
    knowledge_section = _render_knowledge_section()
    role_text = _read_text_with_default(
        ROOT / "docs" / "roles" / "code-writer.md",
        "code_writer_md_default.txt",
    )
    task_packet_section = _render_task_packet_section()
    task_card_data = _read_json_if_exists(TASK_CARD)
    task_card = cast(TaskCard, task_card_data) if isinstance(task_card_data, dict) else cast(TaskCard, {})
    handoff_section = _render_handoff_context_section(task_id, round_num)

    sections: list[tuple[str, str]] = []

    if round_num == 1:
        agents_text = _read_text_with_default(
            ROOT / "AGENTS.md",
            "agents_md_default.txt",
        )
        orchestrator_path = ROOT / "src" / "loop_kit" / "orchestrator.py"
        task_card_section = _render_task_card_section(task_card)
        quickstart_section = _render_quickstart_context_section(task_card)
        prior_context_section = _render_prior_round_context_section(round_num)

        sections = [
            ("=== BEGIN AGENTS.md ===", f"{agents_text}\n=== END AGENTS.md ==="),
            ("=== BEGIN docs/roles/code-writer.md ===", f"{role_text}\n=== END docs/roles/code-writer.md ==="),
            (
                "=== BEGIN FUNCTION INDEX: " + _display_path(orchestrator_path) + " ===",
                f"{_function_index(orchestrator_path)}\n=== END FUNCTION INDEX ===",
            ),
            ("=== QUICKSTART CONTEXT ===", quickstart_section),
            ("=== HANDOFF CONTEXT ===", handoff_section),
            ("=== KNOWLEDGE ===", knowledge_section),
            ("=== TASK PACKET ===", task_packet_section),
        ]
        if task_card_section and task_card_section != "- <none>":
            sections.append(("=== TASK CARD ===", task_card_section))
        if prior_context_section:
            lines = prior_context_section.split("\n", 1)
            sections.append((lines[0], lines[1] if len(lines) > 1 else ""))
    else:
        fix_list_section = _render_fix_list_section(round_num)
        prior_context_section = _render_prior_round_context_section(round_num)

        sections = [
            ("=== BEGIN docs/roles/code-writer.md ===", f"{role_text}\n=== END docs/roles/code-writer.md ==="),
            ("=== HANDOFF CONTEXT ===", handoff_section),
            ("=== KNOWLEDGE ===", knowledge_section),
            ("=== TASK PACKET ===", task_packet_section),
            (f"=== FIX LIST (round {round_num}) ===", f"fixes:\n{fix_list_section}"),
        ]
        if prior_context_section:
            lines = prior_context_section.split("\n", 1)
            sections.append((lines[0], lines[1] if len(lines) > 1 else ""))

    return sections


def _worker_prompt(task_id: str, round_num: int, paths: LoopPaths | None = None) -> str:
    resolved_paths = _resolve_paths(paths)
    template_path = _worker_prompt_template_path(paths=resolved_paths)
    template_text = _read_text_optional(template_path)
    if template_text is not None:
        include_cold_start_context = round_num == 1
        agents_text = (
            _read_text_with_default(
                ROOT / "AGENTS.md",
                "agents_md_default.txt",
            )
            if include_cold_start_context
            else "<warm session: AGENTS.md omitted>"
        )
        role_text = _read_text_with_default(
            ROOT / "docs" / "roles" / "code-writer.md",
            "code_writer_md_default.txt",
        )
        orchestrator_path = ROOT / "src" / "loop_kit" / "orchestrator.py"
        task_card_data = _read_json_if_exists(TASK_CARD)
        task_card = cast(TaskCard, task_card_data) if isinstance(task_card_data, dict) else cast(TaskCard, {})
        task_card_section = _render_task_card_section(task_card) if include_cold_start_context else ""
        prior_context_section = _render_prior_round_context_section(round_num)
        quickstart_section = (
            _render_quickstart_context_section(task_card)
            if include_cold_start_context
            else "- warm session path; quickstart context is intentionally omitted"
        )
        handoff_section = _render_handoff_context_section(task_id, round_num, paths=resolved_paths)
        knowledge_section = _render_knowledge_section()
        task_packet_section = _render_task_packet_section()
        context = {
            "task_id": task_id,
            "round_num": str(round_num),
            "work_report_path": _display_path(resolved_paths.work_report),
            "agents_md": agents_text,
            "role_md": role_text,
            "orchestrator_path": _display_path(orchestrator_path),
            "function_index": (
                _function_index(orchestrator_path)
                if include_cold_start_context
                else "<warm session: function index omitted>"
            ),
            "quickstart_section": quickstart_section,
            "handoff_section": handoff_section,
            "knowledge_section": knowledge_section,
            "task_packet_section": task_packet_section,
            "task_card_section": task_card_section,
            "prior_context_section": prior_context_section or "",
        }
        return _render_prompt_template(template_path=template_path, context=context)

    header = (
        f"Role: code-writer worker for PM loop.\n"
        f"Current task_id: {task_id}, round: {round_num}.\n"
        f"Execute the contract below and only finish after writing {_display_path(resolved_paths.work_report)}."
    )
    sections = _build_prompt_sections(task_id, round_num)
    result = header + "\n\n" + _join_prompt_sections(sections)
    if round_num > 1 and not _render_prior_round_context_section(round_num):
        result += "\n\n"
    return result


def _reviewer_prompt(task_id: str, round_num: int, paths: LoopPaths | None = None) -> str:
    resolved_paths = _resolve_paths(paths)
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
        "handoff_section": _render_handoff_context_section(task_id, round_num, paths=resolved_paths),
        "review_report_path": _display_path(resolved_paths.review_report),
    }
    return _render_prompt_template(
        template_path=_reviewer_prompt_template_path(paths=resolved_paths),
        context=context,
    )


# ── state ───────────────────────────────────────────────────────────
STATE_SCHEMA_VERSION = 1
STATE_IDLE = "idle"
STATE_AWAITING_WORK = "awaiting_work"
STATE_AWAITING_REVIEW = "awaiting_review"
STATE_DONE = "done"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_DONE = "done"
TASK_STATUS_BLOCKED = "blocked"


def _default_state(task_id: str | None = None, round_num: int = 0) -> dict:
    """Return a fresh state dict with current schema version."""
    return {
        "version": STATE_SCHEMA_VERSION,
        "state": STATE_IDLE,
        "round": round_num,
        "task_id": task_id,
    }


def _load_state(paths: LoopPaths | None = None) -> dict:
    resolved_paths = _resolve_paths(paths)
    state_file = resolved_paths.state
    state_backup = resolved_paths.dir / ".state.json.bak"
    default_state = _default_state()
    if not state_file.exists():
        return default_state.copy()

    def _load_backup_state() -> dict | None:
        if not state_backup.exists():
            return None
        try:
            backup_data = json.loads(state_backup.read_text(encoding="utf-8"))
        except json.JSONDecodeError as backup_err:
            _log(f"Warning: backup state file is corrupted: {backup_err}.")
            return None
        except OSError as backup_err:
            _log(f"Warning: unable to read backup state file: {backup_err}.")
            return None
        if not isinstance(backup_data, dict):
            _log("Warning: backup state file root must be a JSON object. Ignoring backup.")
            return None
        return backup_data

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        backup_state = _load_backup_state()
        if backup_state is not None:
            print("state.json corrupted, recovered from backup", file=sys.stderr)
            _atomic_write_json(state_file, backup_state)
            return backup_state
        _log(f"Warning: state.json is corrupted: {e}. Using fresh default state.")
        return default_state.copy()
    except OSError as e:
        backup_state = _load_backup_state()
        if backup_state is not None:
            print("state.json corrupted, recovered from backup", file=sys.stderr)
            _atomic_write_json(state_file, backup_state)
            return backup_state
        _log(f"Warning: unable to read state.json: {e}. Using fresh default state.")
        return default_state.copy()
    if not isinstance(data, dict):
        _log("Warning: state.json root must be a JSON object. Using fresh default state.")
        return default_state.copy()

    # Migration: ensure version field exists and is current
    version = data.get("version", 0)
    if version != STATE_SCHEMA_VERSION:
        # Migrate: add missing fields with defaults
        migrated = dict(data)  # shallow copy
        migrated["version"] = STATE_SCHEMA_VERSION

        # Ensure core fields exist
        if "state" not in migrated:
            migrated["state"] = STATE_IDLE
        if "round" not in migrated:
            migrated["round"] = 0
        if "task_id" not in migrated:
            migrated["task_id"] = None

        _log(f"State schema migrated from version {version} to {STATE_SCHEMA_VERSION}.")
        return migrated

    return data


def _atomic_write_json(path: Path, data: object) -> None:
    """Write *data* as JSON to *path* atomically (write-then-rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            tmp.replace(path)
        except PermissionError:
            if os.name == "nt":
                import time

                time.sleep(0.05)
                tmp.replace(path)
            else:
                raise
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _save_state(state: dict, paths: LoopPaths | None = None) -> None:
    resolved_paths = _resolve_paths(paths)
    state_file = resolved_paths.state
    state_backup = resolved_paths.dir / ".state.json.bak"
    previous_state: dict | None = None
    if state_file.exists():
        previous = _read_json_if_exists(state_file)
        if isinstance(previous, dict):
            previous_state = previous
        state_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(state_file, state_backup)
    _atomic_write_json(state_file, state)
    from_state = previous_state.get("state") if isinstance(previous_state, dict) else None
    from_round = previous_state.get("round") if isinstance(previous_state, dict) else None
    to_state = state.get("state")
    to_round = state.get("round")
    if from_state != to_state or from_round != to_round:
        task_id_raw = state.get("task_id")
        task_id = task_id_raw if isinstance(task_id_raw, str) and task_id_raw else None
        round_num = to_round if isinstance(to_round, int) else None
        _feed_event(
            FEED_STATE_TRANSITION,
            data=_feed_data(
                task_id=task_id,
                round_num=round_num,
                role="orchestrator",
                from_state=from_state,
                to_state=to_state,
                from_round=from_round,
                to_round=to_round,
            ),
        )


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


def _reset_bus() -> None:
    """Remove stale bus files from a previous run."""
    removed = 0
    for f in _RESETTABLE_FILES:
        if f.is_file():
            f.unlink()
            removed += 1
    if removed:
        _log(f"Reset: removed {removed} stale bus file(s)")


def _sync_task_card(task_path: str, paths: LoopPaths | None = None) -> None:
    """Copy external task card to .loop/task_card.json if it lives elsewhere."""
    resolved_paths = _resolve_paths(paths)
    task_card_path = resolved_paths.task_card
    src = Path(task_path)
    if not src.is_file():
        return
    try:
        if src.resolve() == task_card_path.resolve():
            return
    except OSError:
        pass
    task_card_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    _log(f"Synced task card: {src} -> {task_card_path}")


def _task_card_status_targets(task_path: str, paths: LoopPaths | None = None) -> list[Path]:
    resolved_paths = _resolve_paths(paths)
    targets = [resolved_paths.task_card]
    source = Path(task_path)
    try:
        if _normalized_abs(source) == _normalized_abs(resolved_paths.task_card):
            return targets
    except OSError:
        pass
    targets.append(source)
    return targets


def _write_task_card_status(task_path: str, status: str, paths: LoopPaths | None = None) -> None:
    updated_targets: list[str] = []
    for target in _task_card_status_targets(task_path, paths=paths):
        payload = _read_json_if_exists(target)
        if not isinstance(payload, dict):
            continue
        current_status = payload.get("status")
        if current_status == status:
            continue
        payload["status"] = status
        _atomic_write_json(target, payload)
        updated_targets.append(_display_path(target))
    if updated_targets:
        _log(f"Task card status -> {status}: {', '.join(updated_targets)}")


def _resolve_task_path(task_ref: str | None) -> str | None:
    """Resolve a task ID or path to an absolute task card path.

    Accepts:
      - Full path to a JSON file
      - A task ID like 'T-601' -> finds .loop/tasks/T-601-*.json
      - None -> returns None (caller falls back to default)
    """
    if task_ref is None:
        return None
    p = Path(task_ref)
    if p.is_file():
        return str(p)
    if _TASKS_DIR.is_dir():
        escaped = task_ref.translate(str.maketrans({"[": "[[]", "]": "[]]", "*": "[*]", "?": "[?]"}))
        matches = sorted(_TASKS_DIR.glob(f"{escaped}-*.json"))
        if matches:
            return str(matches[0])
    return task_ref


def _load_config() -> dict:
    """Load .loop/config.json defaults (worker_backend, reviewer_backend, etc.)."""
    if not _CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _enforce_clean_worktree_or_exit(*, allow_dirty: bool) -> None:
    try:
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
        raise DirtyWorktreeError("Dirty worktree")
    except DirtyWorktreeError:
        sys.exit(EXIT_DIRTY_WORKTREE)
    except LoopKitError:
        sys.exit(EXIT_GENERAL_ERROR)


def _validate_report(
    report: WorkReport | ReviewReport,
    *,
    expected_task_id: str,
    expected_round: int,
    schema: Literal["work_report", "review_report"],
) -> str | None:
    if schema == "work_report":
        required_types: dict[str, type] = {
            "task_id": str,
            "head_sha": str,
            "round": int,
        }
        prefix = "work_report"
    elif schema == "review_report":
        required_types = {
            "task_id": str,
            "round": int,
            "decision": str,
        }
        prefix = "review_report"
    else:
        raise ValueError(f"Unknown schema: {schema}")

    for field_name, typ in required_types.items():
        if field_name not in report:
            return f"{prefix} missing required field '{field_name}'"
        value = report[field_name]
        if typ is int:
            if type(value) is not int:
                return f"{prefix} field '{field_name}' must be int, got {type(value).__name__}"
        elif not isinstance(value, typ):
            return f"{prefix} field '{field_name}' must be {typ.__name__}, got {type(value).__name__}"
        if typ is str and not value.strip():
            return f"{prefix} field '{field_name}' must be non-empty"

    if schema == "work_report":
        for list_field in ("files_changed", "tests"):
            if list_field in report and not isinstance(report[list_field], list):
                return f"{prefix} field '{list_field}' must be a list, got {type(report[list_field]).__name__}"
    elif schema == "review_report":
        if report["decision"] not in {"approve", "changes_required"}:
            return (
                f"{prefix} field 'decision' must be one of "
                "{{'approve', 'changes_required'}}, "
                f"got {report['decision']!r}"
            )

    if report["task_id"] != expected_task_id:
        return f"{prefix} field 'task_id' mismatch: expected {expected_task_id!r}, got {report['task_id']!r}"
    if report["round"] != expected_round:
        return f"{prefix} field 'round' mismatch: expected {expected_round}, got {report['round']!r}"
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
    start_time = time.monotonic()
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
        elapsed = time.monotonic() - start_time
        if timeout_sec and elapsed >= timeout_sec:
            _log(f"Timeout ({timeout_sec}s) waiting for {path.name}")
            return None
        if elapsed >= _WAIT_SAFETY_CAP_SEC:
            _log(f"Safety cap (24h) reached waiting for {path.name}")
            return None
        time.sleep(POLL_INTERVAL_SEC)


def _fail_with_state(
    state: dict,
    outcome: str,
    message: str,
    exit_code: int = EXIT_GENERAL_ERROR,
    task_path: str | None = None,
    paths: LoopPaths | None = None,
) -> None:
    _log(message)
    print(f"  Error: {message}", file=sys.stderr)
    state["state"] = STATE_DONE
    state["outcome"] = outcome
    state["failed_at"] = _ts()
    state["error"] = message
    _save_state(state, paths=paths)
    _write_task_card_status(task_path or str(_resolve_paths(paths).task_card), TASK_STATUS_BLOCKED, paths=paths)
    try:
        # Map exit code to appropriate exception type
        if exit_code == EXIT_VALIDATION_ERROR:
            raise ValidationError(message)
        elif exit_code == EXIT_TIMEOUT:
            raise DispatchError(message)
        elif exit_code == EXIT_DIRTY_WORKTREE:
            raise DirtyWorktreeError(message)
        elif exit_code == EXIT_LOCK_FAILURE:
            raise StateError(message)
        elif exit_code == EXIT_INTERRUPTED:
            # Should not happen in normal flow, but map to base
            raise LoopKitError(message)
        else:
            # EXIT_GENERAL_ERROR or unknown -> ConfigError or LoopKitError?
            # Use ConfigError for config-related failures, LoopKitError for others
            raise ConfigError(message) if "config" in outcome.lower() else LoopKitError(message)
    except LoopKitError:
        # This function is an exit point; directly exit with the original exit_code
        sys.exit(exit_code)


def _write_template_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _empty_module_map() -> dict:
    return {
        "files": [],
        "generated_at": _ts(),
        "total_files": 0,
    }


def _default_project_facts_content() -> str:
    return (
        "# Stable project facts\n"
        "- single-file rule: all production logic lives in src/loop_kit/orchestrator.py\n"
        "- subprocess-per-round: each review round runs in a dedicated subprocess\n"
    )


def _default_pitfalls_content() -> str:
    return (
        "# Known pitfalls\n"
        "- lock stale after crash can be misleading; confirm no live orchestrator PID before manual cleanup\n"
        "- Windows replace needs retry when antivirus/indexers hold short file locks\n"
    )


def _default_patterns_content() -> str:
    example = {
        "pattern": "Example: run uv tests after meaningful orchestrator edits",
        "category": "example",
        "confidence": 0.0,
        "last_verified": _ts(),
    }
    return json.dumps(example, ensure_ascii=False) + "\n"


def _parse_module_exports_and_docstring(text: str, rel_path: str) -> tuple[list[str], str]:
    try:
        tree = ast.parse(text, filename=rel_path)
    except SyntaxError:
        return [], ""

    exports: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            exports.append(f"def {node.name}:L{node.lineno}")
        elif isinstance(node, ast.AsyncFunctionDef):
            exports.append(f"async def {node.name}:L{node.lineno}")
        elif isinstance(node, ast.ClassDef):
            exports.append(f"class {node.name}:L{node.lineno}")

    module_docstring = ast.get_docstring(tree) or ""
    first_line = module_docstring.splitlines()[0].strip() if module_docstring else ""
    return exports, first_line


def _index_module_file(path: Path, rel_path: str, stat_result: os.stat_result) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""

    exports, docstring = _parse_module_exports_and_docstring(text, rel_path)
    return {
        "path": rel_path,
        "exports": exports,
        "docstring": docstring,
        "loc": len(text.splitlines()),
        "size_bytes": stat_result.st_size,
        "last_modified": stat_result.st_mtime_ns,
    }


def _load_existing_module_map_entries() -> dict[str, dict]:
    data = _read_json_if_exists(_MODULE_MAP_FILE)
    if not isinstance(data, dict):
        return {}
    raw_files = data.get("files")
    if not isinstance(raw_files, list):
        return {}

    entries: dict[str, dict] = {}
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        rel_path = item.get("path")
        if not isinstance(rel_path, str) or not rel_path:
            continue
        entries[rel_path] = item
    return entries


def _can_reuse_module_entry(entry: object, rel_path: str, *, size_bytes: int, last_modified: int) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("path") != rel_path:
        return False
    if entry.get("size_bytes") != size_bytes or entry.get("last_modified") != last_modified:
        return False
    exports = entry.get("exports")
    if not isinstance(exports, list) or not all(isinstance(name, str) for name in exports):
        return False
    if not isinstance(entry.get("docstring"), str):
        return False
    if not isinstance(entry.get("loc"), int):
        return False
    return True


def _build_module_entry(path: Path, existing_entries: dict[str, dict]) -> dict | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None

    rel_path = path.relative_to(ROOT).as_posix()
    existing = existing_entries.get(rel_path)
    if _can_reuse_module_entry(
        existing,
        rel_path,
        size_bytes=stat_result.st_size,
        last_modified=stat_result.st_mtime_ns,
    ):
        return dict(existing)
    return _index_module_file(path, rel_path, stat_result)


def cmd_index() -> None:
    _CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    source_dir = ROOT / "src" / "loop_kit"
    existing_entries = _load_existing_module_map_entries()

    files: list[dict] = []
    for module_path in sorted(source_dir.rglob("*.py")):
        if not module_path.is_file():
            continue
        entry = _build_module_entry(module_path, existing_entries)
        if entry is not None:
            files.append(entry)

    payload = {
        "files": files,
        "generated_at": _ts(),
        "total_files": len(files),
    }
    _atomic_write_json(_MODULE_MAP_FILE, payload)
    _log(f"Module index updated: {_display_path(_MODULE_MAP_FILE)} ({len(files)} files)")
    print(f"  Indexed: {len(files)} files -> {_display_path(_MODULE_MAP_FILE)}")


# ── init ────────────────────────────────────────────────────────────
def cmd_init(paths: LoopPaths | None = None) -> None:
    resolved_paths = _resolve_paths(paths)
    loop_dir = resolved_paths.dir
    logs_dir = resolved_paths.logs
    runtime_dir = loop_dir / "runtime"
    archive_dir = resolved_paths.archive
    handoff_dir = loop_dir / "handoff"
    context_dir = loop_dir / "context"
    loop_dir.mkdir(exist_ok=True)
    (loop_dir / "examples").mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)
    runtime_dir.mkdir(exist_ok=True)
    archive_dir.mkdir(exist_ok=True)
    handoff_dir.mkdir(exist_ok=True)
    context_dir.mkdir(exist_ok=True)
    templates_dir = _loop_templates_dir(paths=resolved_paths)
    templates_dir.mkdir(exist_ok=True)
    _log(f"Initialized loop directory: {loop_dir}")
    print(f"  Created: {loop_dir}")
    print(f"  Created: {logs_dir}")
    print(f"  Created: {runtime_dir}")
    print(f"  Created: {archive_dir}")
    print(f"  Created: {handoff_dir}")
    print(f"  Created: {context_dir}")
    print(f"  Created: {templates_dir}")
    if not _MODULE_MAP_FILE.exists():
        _atomic_write_json(_MODULE_MAP_FILE, _empty_module_map())
        print(f"  Created: {_MODULE_MAP_FILE}")
    if _write_template_if_missing(_PROJECT_FACTS_FILE, _default_project_facts_content()):
        print(f"  Created: {_PROJECT_FACTS_FILE}")
    if _write_template_if_missing(_PITFALLS_FILE, _default_pitfalls_content()):
        print(f"  Created: {_PITFALLS_FILE}")
    if _write_template_if_missing(_PATTERNS_FILE, _default_patterns_content()):
        print(f"  Created: {_PATTERNS_FILE}")
    # copy example task card if not present
    example = loop_dir / "examples" / "task_card.json"
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
    worker_template = _worker_prompt_template_path(paths=resolved_paths)
    if _write_template_if_missing(worker_template, DEFAULT_WORKER_PROMPT_TEMPLATE + "\n"):
        print(f"  Created: {worker_template}")
    reviewer_template = _reviewer_prompt_template_path(paths=resolved_paths)
    if _write_template_if_missing(reviewer_template, DEFAULT_REVIEWER_PROMPT_TEMPLATE + "\n"):
        print(f"  Created: {reviewer_template}")


# ── status ──────────────────────────────────────────────────────────
def cmd_status(paths: LoopPaths | None = None) -> None:
    resolved_paths = _resolve_paths(paths)
    state = _load_state(paths=resolved_paths)
    print(f"State: {state.get('state', 'unknown')}")
    print(f"Round: {state.get('round', 0)}")
    task_id = state.get("task_id")
    if task_id:
        print(f"Task ID: {task_id}")
    outcome = state.get("outcome")
    if outcome:
        print(f"Outcome: {outcome}")
    print()
    print("Bus files:")
    for p in [
        resolved_paths.task_card,
        resolved_paths.work_report,
        resolved_paths.review_request,
        resolved_paths.review_report,
        resolved_paths.fix_list,
    ]:
        marker = "EXISTS" if p.exists() else "missing"
        print(f"  {p.name}: {marker}")
    print()
    project_facts = _load_project_facts()
    pitfalls = _load_pitfalls()
    patterns, stale_count = _load_patterns_with_governance(persist=False)
    high_conf_count = sum(
        1 for entry in patterns if _coerce_confidence(entry.get("confidence"), default=0.0) >= PATTERN_HIGH_CONFIDENCE
    )
    print("Context files:")
    print(
        "  "
        f"{_PROJECT_FACTS_FILE.name}: "
        f"{'EXISTS' if _PROJECT_FACTS_FILE.exists() else 'missing'} "
        f"(facts={len(project_facts)})"
    )
    print(f"  {_PITFALLS_FILE.name}: {'EXISTS' if _PITFALLS_FILE.exists() else 'missing'} (pitfalls={len(pitfalls)})")
    print(
        "  "
        f"{_PATTERNS_FILE.name}: "
        f"{'EXISTS' if _PATTERNS_FILE.exists() else 'missing'} "
        f"(entries={len(patterns)}, high_confidence={high_conf_count}, stale={stale_count})"
    )
    print()
    print("Heartbeats:")
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


def cmd_archive(task_id: str, restore: str | None = None, paths: LoopPaths | None = None) -> None:
    resolved_paths = _resolve_paths(paths)
    try:
        if ".." in task_id or "/" in task_id or "\\" in task_id:
            print("Error: invalid task_id (path traversal not allowed)", file=sys.stderr)
            raise LoopKitError("Invalid task_id")
        archive_dir = _task_archive_dir(task_id, paths=resolved_paths)
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
            raise LoopKitError("Restore path escapes archive")
        if not src.exists():
            print(
                f"Error: archive file not found for task_id={task_id}: {src}",
                file=sys.stderr,
            )
            raise LoopKitError("Archive file not found")
        target_name = _restore_target_name_from_archive(src.stem)
        dest = resolved_paths.dir / target_name
        shutil.copy2(src, dest)
        print(f"Restored {src.name} -> {dest}")
    except ValidationError:
        sys.exit(EXIT_VALIDATION_ERROR)
    except LoopKitError:
        sys.exit(EXIT_GENERAL_ERROR)


# ── extract-diff ────────────────────────────────────────────────────
def cmd_extract_diff(base: str, head: str) -> None:
    try:
        for ref in (base, head):
            if not _is_valid_ref(ref):
                print(f"Error: invalid git ref: {ref!r}", file=sys.stderr)
                raise LoopKitError(f"Invalid git ref: {ref}")
        print(_diff(base, head))
    except ValidationError:
        sys.exit(EXIT_VALIDATION_ERROR)
    except LoopKitError:
        sys.exit(EXIT_GENERAL_ERROR)


def cmd_heartbeat(role: str, interval: int) -> None:
    role = role.lower().strip()
    if role not in {"worker", "reviewer"}:
        print(f"Error: invalid role: {role}", file=sys.stderr)
        raise ValidationError(f"Invalid role: {role}")
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
            _feed_event(
                FEED_HEARTBEAT,
                data=_feed_data(
                    role=role,
                    source="manual",
                    pid=payload["pid"],
                    updated_at=payload["updated_at"],
                ),
            )
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
def _load_task_card(task_path: str) -> tuple[Path, TaskCard, str]:
    try:
        tp = Path(task_path)
        if not tp.exists():
            print(f"Error: task card not found: {tp}", file=sys.stderr)
            raise ConfigError(f"Task card not found: {tp}")
        try:
            task_card = json.loads(tp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"Error: task card at {tp} contains invalid JSON: {e}", file=sys.stderr)
            raise ConfigError(f"Invalid JSON in task card: {e}") from e
        except OSError as e:
            print(f"Error: unable to read task card at {tp}: {e}", file=sys.stderr)
            raise ConfigError(f"Cannot read task card: {e}") from e
        if not isinstance(task_card, dict):
            print(f"Error: task card must be a JSON object: {tp}", file=sys.stderr)
            raise ConfigError("Task card must be a JSON object")
        task_card_typed = cast(TaskCard, task_card)
        task_id = cast(str, task_card_typed.get("task_id", "UNKNOWN"))
        return tp, task_card_typed, task_id
    except ConfigError:
        sys.exit(EXIT_GENERAL_ERROR)
    except LoopKitError:
        sys.exit(EXIT_GENERAL_ERROR)


def _sync_task_card_to_bus(task_path: str, round_num: int = 1, paths: LoopPaths | None = None) -> tuple[TaskCard, str]:
    resolved_paths = _resolve_paths(paths)
    tp, task_card, task_id = _load_task_card(task_path)
    if _normalized_abs(tp) != _normalized_abs(resolved_paths.task_card):
        _archive_bus_file(resolved_paths.task_card, task_id, round_num, "task_card")
        shutil.copy2(tp, resolved_paths.task_card)
    return task_card, task_id


def _single_round_subprocess_cmd(
    *,
    config: RunConfig,
    round_num: int,
    paths: LoopPaths | None = None,
) -> list[str]:
    resolved_paths = _resolve_paths(paths)
    cmd = [
        sys.executable,
        "-m",
        "loop_kit",
        "run",
        "--single-round",
        "--round",
        str(round_num),
        "--loop-dir",
        _display_path(resolved_paths.dir),
        "--task",
        str(resolved_paths.task_card),
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
        "--max-session-rounds",
        str(config.max_session_rounds),
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


def _normalize_sessions_map(value: object) -> dict[str, dict[str, str | int]]:
    normalized: dict[str, dict[str, str | int]] = {}
    if not isinstance(value, dict):
        return normalized
    for role in _SESSION_ROLES:
        raw_entry = value.get(role)
        if not isinstance(raw_entry, dict):
            continue
        session_id_raw = raw_entry.get("session_id")
        backend_raw = raw_entry.get("backend")
        if not isinstance(session_id_raw, str) or not session_id_raw.strip():
            continue
        if not isinstance(backend_raw, str) or not backend_raw.strip():
            continue
        entry: dict[str, str | int] = {
            "session_id": session_id_raw.strip(),
            "backend": backend_raw.strip().lower(),
        }
        started_round_raw = raw_entry.get("started_round")
        if isinstance(started_round_raw, int) and started_round_raw >= 1:
            entry["started_round"] = started_round_raw
        normalized[role] = entry
    return normalized


def _clear_sessions(state: dict) -> bool:
    normalized = _normalize_sessions_map(state.get("sessions"))
    had_meaningful_data = bool(normalized) or (state.get("sessions") is not None and state.get("sessions") != {})
    state["sessions"] = {}
    return had_meaningful_data


def _session_resume_id(state: dict, *, role: str, backend: str) -> str | None:
    sessions = _normalize_sessions_map(state.get("sessions"))
    state["sessions"] = sessions
    entry = sessions.get(role)
    if not isinstance(entry, dict):
        return None
    backend_key = backend.strip().lower()
    if entry.get("backend") != backend_key:
        return None
    session_id = entry.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    return session_id


def _session_started_round(entry: dict[str, str | int], *, round_num: int) -> int:
    started_round_raw = entry.get("started_round")
    if isinstance(started_round_raw, int) and started_round_raw >= 1:
        return started_round_raw
    return max(1, round_num - 1)


def _store_session(state: dict, *, role: str, backend: str, session_id: str | None, round_num: int) -> bool:
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    sessions = _normalize_sessions_map(state.get("sessions"))
    normalized_session_id = session_id.strip()
    existing = sessions.get(role)
    started_round = round_num
    if isinstance(existing, dict):
        existing_session_id = existing.get("session_id")
        if isinstance(existing_session_id, str) and existing_session_id.strip() == normalized_session_id:
            started_round = _session_started_round(existing, round_num=round_num)
    next_entry: dict[str, str | int] = {
        "session_id": normalized_session_id,
        "backend": backend.strip().lower(),
        "started_round": started_round,
    }
    if sessions.get(role) == next_entry:
        state["sessions"] = sessions
        return False
    sessions[role] = next_entry
    state["sessions"] = sessions
    return True


def _invalidate_sessions_for_dispatch(
    state: dict,
    *,
    role: str,
    task_id: str,
    round_num: int,
) -> bool:
    state["sessions"] = _normalize_sessions_map(state.get("sessions"))
    if role != "worker":
        return False
    sessions = cast(dict[str, dict[str, str | int]], state.get("sessions"))
    if not sessions:
        return False

    state_task_id = state.get("task_id")
    if isinstance(state_task_id, str) and state_task_id and state_task_id != task_id:
        _log(f"Clearing dispatch sessions: task_id changed (state={state_task_id!r}, current={task_id!r})")
        _clear_sessions(state)
        return True

    if round_num == 1:
        _log("Clearing dispatch sessions: round reset to 1")
        _clear_sessions(state)
        return True

    state_base_sha = state.get("base_sha")
    if not isinstance(state_base_sha, str) or not state_base_sha:
        return False
    try:
        current_head = _current_sha()
    except RuntimeError as e:
        _log(f"Warning: unable to compare state base_sha to HEAD for session invalidation: {e}")
        return False
    if state_base_sha == current_head:
        return False
    state_head_sha = state.get("head_sha")
    if isinstance(state_head_sha, str) and state_head_sha == current_head:
        return False
    _log(
        "Clearing dispatch sessions: state base_sha differs from current HEAD "
        f"(base_sha={state_base_sha}, head={current_head})"
    )
    _clear_sessions(state)
    return True


def _auto_dispatch_role(
    role: str,
    prompt: str,
    config: RunConfig,
    task_id: str,
    round_num: int,
    artifact_path: Path,
    state: dict | None = None,
) -> dict | None:
    if not config.auto_dispatch:
        return None
    backend = (config.worker_backend if role == "worker" else config.reviewer_backend).strip().lower()
    current_state = state if isinstance(state, dict) else _load_state()
    state_updated = _invalidate_sessions_for_dispatch(
        current_state,
        role=role,
        task_id=task_id,
        round_num=round_num,
    )
    if state_updated:
        _save_state(current_state)
    resume_session_id = _session_resume_id(current_state, role=role, backend=backend)
    candidate_session_id = resume_session_id
    resume_status = "resume_miss"
    session_started_round: int | None = None
    if resume_session_id:
        sessions = _normalize_sessions_map(current_state.get("sessions"))
        entry = sessions.get(role)
        if isinstance(entry, dict):
            session_started_round = _session_started_round(entry, round_num=round_num)
        if config.max_session_rounds > 0 and session_started_round is not None:
            if round_num - session_started_round >= config.max_session_rounds:
                resume_status = "resume_rotated"
                _log(
                    f"{role} session rotation triggered: started_round={session_started_round}, "
                    f"round={round_num}, max_session_rounds={config.max_session_rounds}"
                )
                resume_session_id = None
            else:
                resume_status = "resume_hit"
        else:
            resume_status = "resume_hit"
    _feed_event(
        FEED_DISPATCH_RESUME,
        data=_feed_data(
            task_id=task_id,
            round_num=round_num,
            role=role,
            backend=backend,
            status=resume_status,
            session_id=candidate_session_id,
            session_started_round=session_started_round,
            max_session_rounds=config.max_session_rounds,
        ),
    )
    dispatch_session_id: str | None = None
    dispatch_started_at = time.monotonic()

    def _dispatch_call() -> None:
        nonlocal dispatch_session_id
        dispatch_session_id = _run_auto_dispatch(
            role=role,
            backend=backend,
            prompt=prompt,
            timeout_sec=config.dispatch_timeout,
            verbose=config.verbose,
            dispatch_retries=config.dispatch_retries,
            dispatch_retry_base_sec=config.dispatch_retry_base_sec,
            heartbeat_enabled=config.require_heartbeat,
            heartbeat_ttl_sec=config.heartbeat_ttl,
            task_id=task_id,
            round_num=round_num,
            resume_session_id=resume_session_id,
        )

    try:
        artifact = _dispatch_with_artifact_fallback(
            role=role,
            dispatch_call=_dispatch_call,
            artifact_path=artifact_path,
            task_id=task_id,
            round_num=round_num,
            timeout_sec=config.artifact_timeout,
        )
        _feed_event(
            FEED_DISPATCH_ARTIFACT_WRITTEN,
            data=_feed_data(
                task_id=task_id,
                round_num=round_num,
                role=role,
                backend=backend,
                artifact_path=artifact_path.name,
                latency_ms=max(0, int((time.monotonic() - dispatch_started_at) * 1000)),
                status="written",
            ),
        )
    except PermanentDispatchError:
        if _clear_sessions(current_state):
            _save_state(current_state)
        raise

    if _store_session(
        current_state,
        role=role,
        backend=backend,
        session_id=dispatch_session_id,
        round_num=round_num,
    ):
        _save_state(current_state)
    return artifact


def _wait_for_role_result(
    role: str,
    artifact_path: Path,
    config: RunConfig,
    task_id: str,
    round_num: int,
) -> WorkReport | ReviewReport | None:
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


def _print_blocking_issues(items: list[ReviewIssue]) -> None:
    print(f"  Blocking issues: {len(items)}")
    for issue in items:
        print(f"    - [{issue.get('severity', '?')}] {issue.get('file', '')}: {issue.get('reason', '')}")


def _issue_to_pitfall_line(issue: ReviewIssue) -> str | None:
    severity = str(issue.get("severity", "?")).strip() or "?"
    file_path = str(issue.get("file", "")).strip()
    reason = str(issue.get("reason", "")).strip()
    if not reason and not file_path:
        return None
    if file_path:
        return f"[{severity}] {file_path}: {reason}".strip()
    return f"[{severity}] {reason}".strip()


def _append_pitfalls(lines: list[str]) -> int:
    if not lines:
        return 0
    existing = _read_markdown_knowledge_lines(_PITFALLS_FILE)
    seen = set(existing)
    to_append: list[str] = []
    for line in lines:
        normalized = line.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        to_append.append(normalized)
    if not to_append:
        return 0

    current = _read_text_optional(_PITFALLS_FILE) or ""
    merged_lines = current.splitlines() + [f"- {line}" for line in to_append]
    non_pitfall_lines = [line for line in merged_lines if not line.startswith("- ")]
    pitfall_lines = [line for line in merged_lines if line.startswith("- ")]
    if _KNOWLEDGE_MAX_PITFALL_LINES <= 0:
        pitfall_lines = []
    elif len(pitfall_lines) > _KNOWLEDGE_MAX_PITFALL_LINES:
        pitfall_lines = pitfall_lines[-_KNOWLEDGE_MAX_PITFALL_LINES:]
    current = "\n".join(non_pitfall_lines + pitfall_lines)
    if current:
        current += "\n"
    _PITFALLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PITFALLS_FILE.write_text(current, encoding="utf-8")
    return len(to_append)


def _update_knowledge_on_approval(task_id: str, round_num: int) -> None:
    sources: list[ReviewReport] = []

    current_review_data = _read_json_if_exists(REVIEW_REPORT)
    if (
        isinstance(current_review_data, dict)
        and current_review_data.get("task_id") == task_id
        and current_review_data.get("round") == round_num
    ):
        current_review = cast(ReviewReport, current_review_data)
        sources.append(current_review)

    archived_review_path = _task_archive_dir(task_id) / f"r{round_num}_review_report.json"
    archived_review_data = _read_json_if_exists(archived_review_path)
    if isinstance(archived_review_data, dict) and archived_review_data.get("task_id") == task_id:
        archived_review = cast(ReviewReport, archived_review_data)
        sources.append(archived_review)

    blocking_issues: list[ReviewIssue] = []
    for review in sources:
        raw_blocking = review.get("blocking_issues", [])
        items = [item for item in raw_blocking if isinstance(item, dict)] if isinstance(raw_blocking, list) else []
        if items:
            blocking_issues = cast(list[ReviewIssue], items)
            break
    if not blocking_issues:
        return

    pitfall_lines = [line for issue in blocking_issues if (line := _issue_to_pitfall_line(issue))]
    appended_pitfalls = _append_pitfalls(pitfall_lines)

    existing_patterns, _ = _load_patterns_with_governance(persist=False)
    now_iso = _to_utc_iso8601(datetime.now(UTC))
    appended_patterns = 0
    for issue in blocking_issues:
        pattern_text = str(issue.get("reason", "")).strip()
        if not pattern_text:
            continue
        category = str(issue.get("category", "review_blocking_issue")).strip() or "review_blocking_issue"
        confidence = _coerce_confidence(issue.get("confidence"), default=1.0)
        existing_patterns.append(
            {
                "pattern": pattern_text,
                "category": category,
                "confidence": confidence,
                "last_verified": now_iso,
            }
        )
        appended_patterns += 1
    if _KNOWLEDGE_MAX_PATTERNS <= 0:
        existing_patterns = []
    elif len(existing_patterns) > _KNOWLEDGE_MAX_PATTERNS:
        existing_patterns = existing_patterns[-_KNOWLEDGE_MAX_PATTERNS:]
    _write_patterns_jsonl(existing_patterns)
    _log(
        "Knowledge updated on approval: "
        f"pitfalls+={appended_pitfalls}, patterns+={appended_patterns}, source=review_report.blocking_issues"
    )


def _run_single_round(
    *,
    config: RunConfig,
    round_num: int,
    single_round: bool,
    paths: LoopPaths | None = None,
) -> None:
    # TODO(paths): this function still has direct global path reads/writes; keep _global_paths sync in main loop.
    resolved_paths = _resolve_paths(paths)
    task_packet_path = resolved_paths.dir / "task_packet.json"
    _ = single_round
    task_card, task_id_from_card = _sync_task_card_to_bus(config.task_path, round_num=round_num, paths=resolved_paths)
    _write_task_card_status(config.task_path, TASK_STATUS_IN_PROGRESS, paths=resolved_paths)

    state = _load_state(paths=resolved_paths)
    state_task_id = state.get("task_id")
    state_base_sha = state.get("base_sha")

    def _save_single_round_state() -> None:
        _archive_state_for_round(task_id_from_card, round_num, paths=resolved_paths)
        _save_state(state, paths=resolved_paths)

    def _fail_single_round(outcome: str, message: str, exit_code: int = EXIT_VALIDATION_ERROR) -> None:
        _archive_state_for_round(task_id_from_card, round_num, paths=resolved_paths)
        _fail_with_state(
            state,
            outcome=outcome,
            message=message,
            exit_code=exit_code,
            task_path=config.task_path,
            paths=resolved_paths,
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
                "sessions": {},
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
    _set_feed_round(round_num)

    _log(f"Loaded task card: {task_id}")
    _log(f"Goal: {task_card.get('goal', '<no goal>')}")
    _log(f"Single-round state contract: task_id={task_id} base_sha={base_sha}")

    _clean_stale_state(state, *_STALE_STATE_KEYS[:3])
    if not isinstance(state.get("round_details"), list):
        state["round_details"] = []
    state["sessions"] = _normalize_sessions_map(state.get("sessions"))
    if round_num == 1:
        state["sessions"] = {}
    state["started_at"] = _ts()
    state["round"] = round_num
    state["state"] = STATE_AWAITING_WORK
    _save_single_round_state()
    _feed_event(
        FEED_ROUND_START,
        data=_feed_data(
            task_id=task_id,
            round_num=round_num,
            role="orchestrator",
            mode="single_round",
        ),
    )

    task_packet: TaskPacket = _build_task_packet(task_card, round_num)
    task_packet_path.write_text(
        json.dumps(task_packet, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    worker_prompt = _worker_prompt(task_id, round_num, paths=resolved_paths)
    _prepare_bus_file(resolved_paths.work_report, task_id, round_num, "work_report")
    _prepare_bus_file(resolved_paths.review_report, task_id, round_num, "review_report")

    _print_round_header(round_num, "worker")

    work: WorkReport | None = None
    try:
        work = _auto_dispatch_role(
            role="worker",
            prompt=worker_prompt,
            config=config,
            task_id=task_id,
            round_num=round_num,
            artifact_path=resolved_paths.work_report,
            state=state,
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
            artifact_path=resolved_paths.work_report,
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
        _write_task_card_status(config.task_path, TASK_STATUS_BLOCKED, paths=resolved_paths)
        raise DispatchError("Worker timed out")

    report_error = _validate_report(
        work,
        expected_task_id=task_id,
        expected_round=round_num,
        schema="work_report",
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
    _persist_worker_handoff(
        task_id=task_id,
        round_num=round_num,
        work=work,
        paths=resolved_paths,
    )
    print(f"  Worker completed: {head_sha[:8]}")
    print(f"  Files changed: {', '.join(work.get('files_changed', []))}")

    review_request: ReviewRequest = {
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
    _archive_bus_file(resolved_paths.review_request, task_id, round_num, "review_request")
    resolved_paths.review_request.write_text(
        json.dumps(review_request, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _prepare_bus_file(resolved_paths.review_report, task_id, round_num, "review_report")

    state["state"] = STATE_AWAITING_REVIEW
    state["head_sha"] = head_sha
    _save_single_round_state()

    _print_round_header(round_num, "reviewer")

    review: ReviewReport | None = None
    try:
        review = _auto_dispatch_role(
            role="reviewer",
            prompt=_reviewer_prompt(task_id, round_num, paths=resolved_paths),
            config=config,
            task_id=task_id,
            round_num=round_num,
            artifact_path=resolved_paths.review_report,
            state=state,
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
            artifact_path=resolved_paths.review_report,
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
        _write_task_card_status(config.task_path, TASK_STATUS_BLOCKED, paths=resolved_paths)
        raise DispatchError("Reviewer timed out")

    review_error = _validate_report(
        review,
        expected_task_id=task_id,
        expected_round=round_num,
        schema="review_report",
    )
    if review_error:
        _fail_single_round(
            outcome="invalid_review_report",
            message=review_error,
            exit_code=EXIT_VALIDATION_ERROR,
        )
        return

    _persist_reviewer_handoff(
        task_id=task_id,
        round_num=round_num,
        review=review,
        paths=resolved_paths,
    )
    decision = str(review["decision"])
    _log(f"Reviewer decision: {decision}")
    _feed_event(
        FEED_REVIEW_VERDICT,
        data=_feed_data(
            task_id=task_id,
            round_num=round_num,
            role="reviewer",
            decision=decision,
        ),
    )
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
        try:
            _update_knowledge_on_approval(task_id, round_num)
        except OSError as e:
            _log(f"Warning: failed to update knowledge context on approval: {e}")
        state["state"] = STATE_DONE
        state["outcome"] = "approved"
        _save_single_round_state()
        _write_task_card_status(config.task_path, TASK_STATUS_DONE, paths=resolved_paths)
        print(f"\n{'=' * 60}")
        print(f"  APPROVED at round {round_num}")
        print(f"  base: {base_sha[:8]}  head: {head_sha[:8]}")
        print(f"{'=' * 60}")

        resolved_paths.summary.write_text(
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
        _archive_task_summary(task_id, paths=resolved_paths)
        _log("Task approved. Summary written to .loop/summary.json")
        _feed_event(
            FEED_ROUND_COMPLETE,
            data=_feed_data(
                task_id=task_id,
                round_num=round_num,
                role="orchestrator",
                decision=decision,
                outcome="approved",
            ),
        )
        return

    raw_blocking = review.get("blocking_issues", [])
    blocking = raw_blocking if isinstance(raw_blocking, list) else []
    blocking_items = cast(list[ReviewIssue], [item for item in blocking if isinstance(item, dict)])
    fix_list: FixList = {
        "task_id": task_id,
        "round": round_num + 1,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "fixes": blocking_items,
        "prior_round_notes": work.get("notes", ""),
        "prior_review_non_blocking": review.get("non_blocking_suggestions", []),
    }
    _archive_bus_file(resolved_paths.fix_list, task_id, round_num, "fix_list")
    resolved_paths.fix_list.write_text(
        json.dumps(fix_list, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _prepare_bus_file(resolved_paths.work_report, task_id, round_num, "work_report")

    _print_blocking_issues(blocking_items)
    print(f"  Fix list written to {resolved_paths.fix_list}")
    _feed_event(
        FEED_ROUND_COMPLETE,
        data=_feed_data(
            task_id=task_id,
            round_num=round_num,
            role="orchestrator",
            decision=decision,
            outcome="changes_required",
            next_round=round_num + 1,
        ),
    )

    state["state"] = STATE_AWAITING_WORK
    state["round"] = round_num + 1
    _save_single_round_state()


def _run_multi_round_via_subprocess(
    *,
    config: RunConfig,
    worktree_checked: bool = False,
    resume_from_state: dict | None = None,
    paths: LoopPaths | None = None,
) -> None:
    # TODO(paths): this function still has direct global path reads/writes; keep _global_paths sync in main loop.
    resolved_paths = _resolve_paths(paths)
    if not worktree_checked:
        _enforce_clean_worktree_or_exit(allow_dirty=config.allow_dirty)

    start_round = 1
    task_id = ""
    base_sha = ""
    if resume_from_state is None:
        task_card, task_id = _sync_task_card_to_bus(config.task_path, round_num=1, paths=resolved_paths)
        _set_feed_task_id(task_id)
        _set_feed_round(1)

        _log(f"Loaded task card: {task_id}")
        _log(f"Goal: {task_card.get('goal', '<no goal>')}")

        base_sha = _current_sha()
        _log(f"Base SHA: {base_sha}")

        state = _load_state(paths=resolved_paths)
        _clean_stale_state(state, *_STALE_STATE_KEYS)
        state.update(
            {
                "state": STATE_AWAITING_WORK,
                "round": 1,
                "task_id": task_id,
                "base_sha": base_sha,
                "started_at": _ts(),
                "sessions": {},
            }
        )
        _save_state(state, paths=resolved_paths)
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
                task_path=config.task_path,
            )
            return
        _, task_card, task_id_from_card = _load_task_card(str(resolved_paths.task_card))
        if task_id_from_card != state_task_id:
            _fail_with_state(
                state,
                outcome="state_task_mismatch",
                message=(
                    "task_id mismatch between state.json and task card during resume: "
                    f"state={state_task_id!r} task={task_id_from_card!r}"
                ),
                exit_code=EXIT_VALIDATION_ERROR,
                task_path=config.task_path,
            )
            return
        task_id = state_task_id
        base_sha = state_base_sha
        start_round = state_round
        _set_feed_task_id(task_id)
        _set_feed_round(start_round)
        _log(f"Resuming task: {task_id}")
        _log(f"Resume contract: base_sha={base_sha} round={start_round}")
        _clean_stale_state(state, *_STALE_STATE_KEYS[:3])
        if not isinstance(state.get("round_details"), list):
            state["round_details"] = []
        state["sessions"] = _normalize_sessions_map(state.get("sessions"))
        if start_round == 1:
            state["sessions"] = {}
        state["state"] = STATE_AWAITING_WORK
        state["round"] = start_round
        state["started_at"] = _ts()
        _save_state(state, paths=resolved_paths)

    _write_task_card_status(config.task_path, TASK_STATUS_IN_PROGRESS, paths=resolved_paths)

    _prepare_bus_file(resolved_paths.work_report, task_id, start_round, "work_report")
    _prepare_bus_file(resolved_paths.review_report, task_id, start_round, "review_report")
    for role in ("worker", "reviewer"):
        _dispatch_log_path(role, paths=resolved_paths).unlink(missing_ok=True)

    last_decision = "changes_required"
    interrupted = False
    _interrupted_event = threading.Event()
    current_proc: subprocess.Popen[str] | None = None

    def _outer_sigint_handler(signum: int, frame: object) -> None:
        _interrupted_event.set()
        if current_proc is not None and current_proc.poll() is None:
            _log(f"SIGINT received during round {round_num}; terminating subprocess")
            with contextlib.suppress(OSError):
                current_proc.terminate()

    old_sigint = signal.signal(signal.SIGINT, _outer_sigint_handler)

    try:
        for round_num in range(start_round, config.max_rounds + 1):
            _set_feed_round(round_num)
            if _interrupted_event.is_set():
                interrupted = True
                break

            print(f"\n{'=' * 60}")
            print(f"  ROUND {round_num}/{config.max_rounds}  —  Single-Round Subprocess")
            print(f"{'=' * 60}")
            _archive_bus_file(resolved_paths.state, task_id, round_num, "state")

            cmd = _single_round_subprocess_cmd(
                config=config,
                round_num=round_num,
                paths=resolved_paths,
            )
            _log(f"Launching single-round subprocess: {' '.join(cmd)}")
            if current_proc is not None and current_proc.poll() is None:
                with contextlib.suppress(OSError):
                    current_proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    current_proc.wait(timeout=2)
                current_proc = None

            proc: subprocess.Popen[str] | None = None
            try:
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
            finally:
                if proc is not None and proc.poll() is None:
                    with contextlib.suppress(OSError):
                        proc.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                        proc.wait(timeout=2)
                if current_proc is proc:
                    current_proc = None

            if _interrupted_event.is_set():
                interrupted = True
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
                    task_path=config.task_path,
                )
                return

            _ = _load_task_card(str(resolved_paths.task_card))
            review_data = _read_json_if_exists(resolved_paths.review_report)
            review = cast(ReviewReport, review_data) if isinstance(review_data, dict) else None
            fix_list_data = _read_json_if_exists(resolved_paths.fix_list)
            fix_list = cast(FixList, fix_list_data) if isinstance(fix_list_data, dict) else None
            state = _load_state(paths=resolved_paths)

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
                    task_path=config.task_path,
                )
                return

            if state.get("state") == STATE_DONE and state.get("outcome") == "approved":
                _write_task_card_status(config.task_path, TASK_STATUS_DONE, paths=resolved_paths)
                _archive_task_summary(task_id, paths=resolved_paths)
                _log(f"Task approved via state contract at round={round_num}")
                return

            if state.get("state") == STATE_AWAITING_WORK and state.get("round") == round_num + 1:
                last_decision = "changes_required"
                if (
                    fix_list is not None
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
                if review is not None:
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
                task_path=config.task_path,
            )
            return
    finally:
        if current_proc is not None and current_proc.poll() is None:
            with contextlib.suppress(OSError):
                current_proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                current_proc.wait(timeout=2)
        signal.signal(signal.SIGINT, old_sigint)

    if interrupted:
        _fail_with_state(
            _load_state(paths=resolved_paths),
            outcome="interrupted",
            message="User interrupted (SIGINT)",
            exit_code=EXIT_INTERRUPTED,
            task_path=config.task_path,
            paths=resolved_paths,
        )

    state = _load_state(paths=resolved_paths)
    state["state"] = STATE_DONE
    state["outcome"] = "max_rounds_exhausted"
    _save_state(state, paths=resolved_paths)
    _write_task_card_status(config.task_path, TASK_STATUS_BLOCKED, paths=resolved_paths)
    print(f"\n  MAX ROUNDS ({config.max_rounds}) reached without approval.")
    print(f"  Last review decision: {last_decision}")
    print("  PM should re-evaluate task scope or split the task.")
    raise DispatchError("Max rounds exhausted")


def _main_loop(
    *,
    config: RunConfig,
    worktree_checked: bool = False,
    resume_from_state: dict | None = None,
    paths: LoopPaths | None = None,
) -> None:
    resolved_paths = _resolve_paths(paths)
    global _global_paths
    _global_paths = resolved_paths
    if not _paths_match_globals(resolved_paths):
        _apply_loop_paths(resolved_paths)
    _run_multi_round_via_subprocess(
        config=config,
        worktree_checked=worktree_checked,
        resume_from_state=resume_from_state,
        paths=resolved_paths,
    )


def cmd_run(
    config: RunConfig,
    single_round: bool,
    round_num: int | None,
    resume: bool = False,
    reset: bool = False,
    paths: LoopPaths | None = None,
) -> None:
    resolved_paths = _resolve_paths(paths)
    global _global_paths
    _global_paths = resolved_paths
    if not _paths_match_globals(resolved_paths):
        _apply_loop_paths(resolved_paths)
    try:
        lock: _LoopLock | None = None
        # Single-round subprocesses are spawned by the parent loop which already
        # holds the lock — skip lock acquisition to avoid self-deadlock.
        if not single_round:
            try:
                lock = _acquire_run_lock()
            except RuntimeError as e:
                print(f"Error: {e}", file=sys.stderr)
                raise StateError(str(e)) from e
        try:
            if reset and not single_round:
                _reset_bus()
                _sync_task_card(config.task_path, paths=resolved_paths)
            elif not single_round:
                # Still sync task card even without full reset
                _sync_task_card(config.task_path, paths=resolved_paths)

            # Single-round subprocesses are spawned by the parent loop which already
            # validated the worktree — skip redundant check to avoid duplicate warnings.
            if not single_round:
                _enforce_clean_worktree_or_exit(allow_dirty=config.allow_dirty)

            if resume and single_round:
                print("Error: --resume cannot be combined with --single-round", file=sys.stderr)
                raise ValidationError("--resume cannot be combined with --single-round")

            if single_round:
                if round_num is None or round_num < 1:
                    print("Error: --single-round requires --round N (N >= 1)", file=sys.stderr)
                    raise ValidationError("--single-round requires --round N (N >= 1)")
                _run_single_round(
                    config=config,
                    round_num=round_num,
                    single_round=single_round,
                    paths=resolved_paths,
                )
                return

            if round_num is not None:
                print("Error: --round is only valid together with --single-round", file=sys.stderr)
                raise ValidationError("--round is only valid together with --single-round")

            resume_state: dict | None = None
            if resume:
                resume_state = _load_state(paths=resolved_paths)
                outcome = resume_state.get("outcome")
                state_name = resume_state.get("state")
                if state_name == STATE_DONE and outcome == "approved":
                    _write_task_card_status(config.task_path, TASK_STATUS_DONE, paths=resolved_paths)
                    print(
                        "Resume not needed: state.json already marked done/approved "
                        f"for task_id={resume_state.get('task_id')!r}."
                    )
                    return
                if state_name == STATE_DONE and outcome != "approved":
                    _write_task_card_status(config.task_path, TASK_STATUS_BLOCKED, paths=resolved_paths)
                    error_text = resume_state.get("error") or "<no error details in state.json>"
                    print(
                        "Error: cannot resume because state.json indicates a failed run: "
                        f"outcome={outcome!r} error={error_text}",
                        file=sys.stderr,
                    )
                    print("Re-run without --resume to start a fresh run.", file=sys.stderr)
                    raise ValidationError(f"Cannot resume from failed state: {outcome}")

            _main_loop(
                config=config,
                worktree_checked=True,
                resume_from_state=resume_state,
                paths=resolved_paths,
            )
        finally:
            if lock is not None:
                lock.release()
    except DirtyWorktreeError:
        sys.exit(EXIT_DIRTY_WORKTREE)
    except StateError as e:
        print(f"Error: state error: {e}", file=sys.stderr)
        sys.exit(EXIT_LOCK_FAILURE)
    except DispatchError:
        sys.exit(EXIT_TIMEOUT)
    except ValidationError:
        sys.exit(EXIT_VALIDATION_ERROR)
    except ConfigError:
        sys.exit(EXIT_GENERAL_ERROR)
    except LoopKitError:
        sys.exit(EXIT_GENERAL_ERROR)


# ── CLI ─────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="PM-driven review loop orchestrator",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {importlib.metadata.version('agent-task-runner')}",
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--loop-dir",
        default=".loop",
        help="Loop bus directory (relative values resolve from repo root)",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("init", parents=[shared], help="Create loop directory structure")
    sub.add_parser("index", parents=[shared], help="Generate offline module map for src/loop_kit")

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
    run_p.add_argument("task_ref", nargs="?", default=None, help="Task ID (e.g. T-601) or path to task card JSON")
    run_p.add_argument("--task", default=None, help="Path to task card JSON (overrides positional task_ref)")
    run_p.add_argument("--max-rounds", type=int, default=None, help="Maximum review rounds (default: 3)")
    run_p.add_argument("--timeout", type=int, default=None, help="Per-phase timeout in seconds (0=unlimited)")
    run_p.add_argument(
        "--require-heartbeat", action="store_true", help="Require fresh worker/reviewer heartbeat while waiting"
    )
    run_p.add_argument("--heartbeat-ttl", type=int, default=None, help="Heartbeat freshness threshold in seconds")
    run_p.add_argument(
        "--auto-dispatch",
        action="store_true",
        default=None,
        help="Automatically invoke worker/reviewer backends each round",
    )
    run_p.add_argument(
        "--dispatch-backend",
        choices=[DISPATCH_BACKEND_NATIVE],
        default=None,
        help="Dispatch transport: native subprocess calls",
    )
    run_p.add_argument("--worker-backend", default=None, help="Backend used for auto worker dispatch (native mode)")
    run_p.add_argument(
        "--reviewer-backend",
        default=None,
        help="Backend used for auto reviewer dispatch (native mode)",
    )
    run_p.add_argument(
        "--dispatch-timeout",
        type=int,
        default=None,
        help="Per-dispatch timeout in seconds (default: 0, 0=unlimited)",
    )
    run_p.add_argument(
        "--dispatch-retries",
        type=int,
        default=None,
        help="Retry count for non-zero dispatch exits (default: 2)",
    )
    run_p.add_argument(
        "--dispatch-retry-base-sec",
        type=int,
        default=None,
        help="Base retry backoff seconds (default: 5, max delay: 60)",
    )
    run_p.add_argument(
        "--max-session-rounds",
        type=int,
        default=None,
        help="Max rounds to reuse one backend session before rotating (0 disables rotation)",
    )
    run_p.add_argument(
        "--artifact-timeout",
        type=int,
        default=None,
        help="Post-dispatch artifact timeout in seconds (default: 90)",
    )
    run_p.add_argument("--single-round", action="store_true", help="Run exactly one round and exit")
    run_p.add_argument("--round", type=int, help="Round number for --single-round mode")
    run_p.add_argument("--allow-dirty", action="store_true", help="Allow run to start with dirty tracked git files")
    run_p.add_argument("--resume", action="store_true", help="Resume from .loop/state.json contract")
    run_p.add_argument("--reset", action="store_true", help="Reset stale bus files before running (default: off)")
    run_p.add_argument("--verbose", action="store_true", help="Stream full backend stdout lines during auto-dispatch")

    args = parser.parse_args()
    if args.cmd is None:
        parser.print_help()
        return
    try:
        _configure_loop_paths(args.loop_dir)
        if args.cmd == "init":
            cmd_init()
        elif args.cmd == "index":
            cmd_index()
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
            cfg = _load_config()
            # Resolve task path: --task > positional task_ref > config > default
            raw_ref = args.task if args.task is not None else args.task_ref
            task_path = _resolve_task_path(raw_ref) or str(TASK_CARD)

            def _cfg_val(cli_val, config_key, builtin_default):
                """CLI arg > config.json > builtin default."""
                if cli_val is not None:
                    return cli_val
                v = cfg.get(config_key)
                return v if v is not None else builtin_default

            config = RunConfig(
                task_path=task_path,
                max_rounds=int(_cfg_val(args.max_rounds, "max_rounds", DEFAULT_MAX_ROUNDS)),
                timeout=int(_cfg_val(args.timeout, "timeout", 0)),
                require_heartbeat=args.require_heartbeat,
                heartbeat_ttl=int(_cfg_val(args.heartbeat_ttl, "heartbeat_ttl", DEFAULT_HEARTBEAT_TTL_SEC)),
                auto_dispatch=_cfg_val(args.auto_dispatch, "auto_dispatch", False),
                dispatch_backend=str(_cfg_val(args.dispatch_backend, "dispatch_backend", DEFAULT_DISPATCH_BACKEND)),
                worker_backend=str(_cfg_val(args.worker_backend, "worker_backend", DEFAULT_WORKER_BACKEND)),
                reviewer_backend=str(_cfg_val(args.reviewer_backend, "reviewer_backend", DEFAULT_REVIEWER_BACKEND)),
                dispatch_timeout=int(_cfg_val(args.dispatch_timeout, "dispatch_timeout", DEFAULT_DISPATCH_TIMEOUT_SEC)),
                dispatch_retries=int(_cfg_val(args.dispatch_retries, "dispatch_retries", DEFAULT_DISPATCH_RETRIES)),
                dispatch_retry_base_sec=int(
                    _cfg_val(args.dispatch_retry_base_sec, "dispatch_retry_base_sec", DEFAULT_DISPATCH_RETRY_BASE_SEC)
                ),
                max_session_rounds=int(
                    _cfg_val(args.max_session_rounds, "max_session_rounds", DEFAULT_MAX_SESSION_ROUNDS)
                ),
                artifact_timeout=int(
                    _cfg_val(args.artifact_timeout, "artifact_timeout", DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC)
                ),
                allow_dirty=args.allow_dirty,
                verbose=args.verbose,
            )
            cmd_run(
                config,
                single_round=args.single_round,
                round_num=args.round,
                resume=args.resume,
                reset=args.reset,
            )
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPTED)
    except DirtyWorktreeError:
        sys.exit(EXIT_DIRTY_WORKTREE)
    except StateError as e:
        print(f"Error: state error: {e}", file=sys.stderr)
        sys.exit(EXIT_LOCK_FAILURE)
    except DispatchError:
        sys.exit(EXIT_TIMEOUT)
    except ValidationError:
        sys.exit(EXIT_VALIDATION_ERROR)
    except ConfigError:
        sys.exit(EXIT_GENERAL_ERROR)
    except LoopKitError:
        sys.exit(EXIT_GENERAL_ERROR)


if __name__ == "__main__":
    main()
