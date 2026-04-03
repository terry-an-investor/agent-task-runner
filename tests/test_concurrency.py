from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from loop_kit import orchestrator


def _hold_lock_until_released(
    lock_path: str,
    acquired_event,
    release_event,
    result_queue,
) -> None:
    lock = orchestrator._LoopLock(Path(lock_path))
    try:
        lock.acquire()
        acquired_event.set()
        release_event.wait(10)
        result_queue.put(("ok", "held"))
    except Exception as exc:  # pragma: no cover - exercised via process output
        acquired_event.set()
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        lock.release()


def _acquire_with_timeout(
    lock_path: str,
    start_event,
    timeout_seconds: float,
    result_queue,
) -> None:
    if not start_event.wait(5):
        result_queue.put(("error", "holder did not acquire lock"))
        return

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        lock = orchestrator._LoopLock(Path(lock_path))
        try:
            lock.acquire()
        except RuntimeError:
            continue
        else:
            lock.release()
            result_queue.put(("unexpected", "acquired lock while holder active"))
            return

    try:
        raise TimeoutError(f"timed out after {timeout_seconds}s")
    except TimeoutError as exc:
        result_queue.put(("timeout", str(exc)))


def _acquire_once(lock_path: str, result_queue) -> None:
    lock = orchestrator._LoopLock(Path(lock_path))
    try:
        lock.acquire()
        result_queue.put(("ok", "acquired"))
    except Exception as exc:  # pragma: no cover - exercised via process output
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        lock.release()


def _cleanup_lock_file(lock_path: Path) -> None:
    lock_path.unlink(missing_ok=True)


def test_lock_prevents_concurrent_access(tmp_path: Path) -> None:
    lock_path = tmp_path / ".loop" / "lock"
    ctx = multiprocessing.get_context("spawn")
    acquired_event = ctx.Event()
    release_event = ctx.Event()
    holder_results = ctx.Queue()
    contender_results = ctx.Queue()

    holder = ctx.Process(
        target=_hold_lock_until_released,
        args=(str(lock_path), acquired_event, release_event, holder_results),
    )
    contender = ctx.Process(
        target=_acquire_with_timeout,
        args=(str(lock_path), acquired_event, 1.0, contender_results),
    )

    holder.start()
    contender.start()

    try:
        assert acquired_event.wait(5), "holder never acquired lock"

        contender.join(10)
        assert not contender.is_alive(), "contender process did not finish"
        assert contender.exitcode == 0
        assert contender_results.get(timeout=1)[0] == "timeout"
    finally:
        release_event.set()
        holder.join(10)
        if holder.is_alive():
            holder.terminate()
            holder.join(5)
        _cleanup_lock_file(lock_path)

    assert holder.exitcode == 0
    assert holder_results.get(timeout=1)[0] == "ok"


def test_lock_released_on_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / ".loop" / "lock"
    ctx = multiprocessing.get_context("spawn")
    acquired_event = ctx.Event()
    release_event = ctx.Event()
    holder_results = ctx.Queue()
    contender_results = ctx.Queue()

    holder = ctx.Process(
        target=_hold_lock_until_released,
        args=(str(lock_path), acquired_event, release_event, holder_results),
    )
    contender = ctx.Process(target=_acquire_once, args=(str(lock_path), contender_results))

    holder.start()

    try:
        assert acquired_event.wait(5), "holder never acquired lock"
        release_event.set()

        holder.join(10)
        assert not holder.is_alive(), "holder process did not exit"
        assert holder.exitcode == 0
        assert holder_results.get(timeout=1)[0] == "ok"

        contender.start()
        contender.join(10)
        assert not contender.is_alive(), "contender process did not finish"
        assert contender.exitcode == 0
        assert contender_results.get(timeout=1)[0] == "ok"
    finally:
        if holder.is_alive():
            holder.terminate()
            holder.join(5)
        if contender.is_alive():
            contender.terminate()
            contender.join(5)
        _cleanup_lock_file(lock_path)


def test_lock_fd_closed(tmp_path: Path) -> None:
    lock_path = tmp_path / ".loop" / "lock"
    lock = orchestrator._LoopLock(lock_path)

    with lock:
        handle = lock._handle
        assert handle is not None
        assert not handle.closed

    assert lock._handle is None
    assert handle.closed
    _cleanup_lock_file(lock_path)


def test_lock_shared_process(tmp_path: Path) -> None:
    lock_path = tmp_path / ".loop" / "lock"
    first_holds_lock = threading.Event()
    release_first = threading.Event()
    errors: list[str] = []

    def holder() -> None:
        with orchestrator._LoopLock(lock_path):
            first_holds_lock.set()
            release_first.wait(5)

    def contender() -> None:
        if not first_holds_lock.wait(5):
            errors.append("holder never acquired lock")
            return
        second_lock = orchestrator._LoopLock(lock_path)
        try:
            second_lock.acquire()
        except RuntimeError:
            return
        else:
            second_lock.release()
            errors.append("contender thread unexpectedly acquired lock")

    holder_thread = threading.Thread(target=holder)
    contender_thread = threading.Thread(target=contender)
    holder_thread.start()
    contender_thread.start()
    contender_thread.join(10)
    release_first.set()
    holder_thread.join(10)

    try:
        assert not holder_thread.is_alive()
        assert not contender_thread.is_alive()
        assert not errors
    finally:
        _cleanup_lock_file(lock_path)


@pytest.mark.skipif(os.name == "nt", reason="Unix-specific fcntl path")
def test_unix_lock_primitives_use_fcntl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[int] = []

    def _fake_flock(fd: int, operation: int) -> None:
        _ = fd
        calls.append(operation)

    monkeypatch.setattr(orchestrator.fcntl, "flock", _fake_flock)
    lock_path = tmp_path / ".loop" / "lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        orchestrator._lock_file(handle)
        orchestrator._unlock_file(handle)

    assert calls == [
        orchestrator.fcntl.LOCK_EX | orchestrator.fcntl.LOCK_NB,
        orchestrator.fcntl.LOCK_UN,
    ]
    _cleanup_lock_file(lock_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific msvcrt path")
def test_windows_lock_primitives_use_msvcrt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[int, int]] = []

    def _fake_locking(fd: int, mode: int, nbytes: int) -> None:
        _ = fd
        calls.append((mode, nbytes))

    monkeypatch.setattr(orchestrator.msvcrt, "locking", _fake_locking)
    lock_path = tmp_path / ".loop" / "lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        orchestrator._lock_file(handle)
        orchestrator._unlock_file(handle)

    assert calls == [
        (orchestrator.msvcrt.LK_NBLCK, 1),
        (orchestrator.msvcrt.LK_UNLCK, 1),
    ]
    _cleanup_lock_file(lock_path)
