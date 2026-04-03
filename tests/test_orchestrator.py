from __future__ import annotations

import builtins
import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from loop_kit import orchestrator


class _FakeStdin:
    def __init__(self) -> None:
        self.value = ""
        self.closed = False

    def write(self, text: str) -> int:
        self.value += text
        return len(text)

    def close(self) -> None:
        self.closed = True


class _FakePipe:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        return None


class _FakeProc:
    def __init__(
        self,
        *,
        stdout_lines: list[str],
        stderr_lines: list[str] | None = None,
        returncode: int = 0,
        poll_ready_after: int = 1,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakePipe(stdout_lines)
        self.stderr = _FakePipe(stderr_lines or [])
        self.returncode: int | None = None
        self._returncode = returncode
        self._poll_ready_after = poll_ready_after
        self._poll_calls = 0
        self.kill_called = False
        self.terminate_called = False
        self.wait_called = False
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        self._poll_calls += 1
        if self.returncode is not None:
            return self.returncode
        if self._poll_calls >= self._poll_ready_after:
            self.returncode = self._returncode
            return self.returncode
        return None

    def kill(self) -> None:
        self.kill_called = True
        close = getattr(self.stdin, "close", None)
        if callable(close):
            close()
        self.returncode = -9

    def terminate(self) -> None:
        self.terminate_called = True
        close = getattr(self.stdin, "close", None)
        if callable(close):
            close()
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        self.wait_called = True
        self.wait_timeouts.append(timeout)
        if self.returncode is None:
            self.returncode = self._returncode
        return self.returncode


class _BlockingStdin:
    def __init__(self) -> None:
        self.closed = False
        self._released = threading.Event()

    def write(self, text: str) -> int:
        _ = text
        self._released.wait()
        if self.closed:
            raise OSError("stdin closed")
        return len(text)

    def close(self) -> None:
        self.closed = True
        self._released.set()


class _FakeEvent:
    def __init__(self, *, initially_set: bool = False) -> None:
        self._is_set = initially_set
        self.set_called = False
        self.wait_calls: list[float | None] = []

    def is_set(self) -> bool:
        return self._is_set

    def set(self) -> None:
        self.set_called = True
        self._is_set = True

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(timeout)
        self._is_set = True
        return True


class _FakeThread:
    def __init__(
        self,
        *,
        target=None,
        args: tuple[object, ...] = (),
        daemon: bool | None = None,
        name: str | None = None,
    ) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.name = name
        self.started = False
        self.join_timeouts: list[float | None] = []

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)


def test_agent_command_codex_uses_stdin_and_short_cli_instruction(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_resolve_backend_exe", lambda backend: f"{backend}.exe")
    long_prompt = "PROMPT-LINE-" * 200

    cmd, session_id, stdin_text = orchestrator._agent_command("codex", long_prompt)

    assert cmd[0] == "codex.exe"
    assert "exec" in cmd
    assert "stdin" in cmd[-1].lower()
    assert long_prompt not in " ".join(cmd)
    assert session_id is None
    assert stdin_text == long_prompt


def test_agent_command_codex_uses_resume_session_when_provided(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_resolve_backend_exe", lambda backend: f"{backend}.exe")

    cmd, session_id, stdin_text = orchestrator._agent_command(
        "codex",
        "payload",
        resume_session_id="tid-resume-123",
    )

    assert cmd[0:2] == ["codex.exe", "exec"]
    assert "resume" in cmd
    assert cmd[cmd.index("resume") + 1] == "tid-resume-123"
    assert "--json" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "stdin" in cmd[-1].lower()
    assert session_id == "tid-resume-123"
    assert stdin_text == "payload"


def test_is_invalid_resume_session_error_handles_codex_no_rollout_message() -> None:
    message = "Error: thread/resume failed: no rollout found for thread id deadbeef"
    assert orchestrator._is_invalid_resume_session_error(message) is True


def test_agent_command_claude_passes_prompt_via_stdin(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_resolve_backend_exe", lambda backend: f"{backend}.exe")
    prompt = "claude prompt payload"

    cmd, session_id, stdin_text = orchestrator._agent_command("claude", prompt)

    assert cmd[0] == "claude.exe"
    assert "--session-id" in cmd
    assert cmd[-1] != prompt
    assert isinstance(session_id, str) and session_id
    assert stdin_text == prompt


def test_agent_command_claude_reuses_resume_session_id(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_resolve_backend_exe", lambda backend: f"{backend}.exe")

    def fail_uuid4() -> str:
        raise AssertionError("uuid.uuid4 must not be called when resume_session_id is provided")

    monkeypatch.setattr(orchestrator.uuid, "uuid4", fail_uuid4)

    cmd, session_id, stdin_text = orchestrator._agent_command(
        "claude",
        "claude prompt payload",
        resume_session_id="sid-reuse-456",
    )

    assert cmd[0] == "claude.exe"
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "sid-reuse-456"
    assert session_id == "sid-reuse-456"
    assert stdin_text == "claude prompt payload"


def test_agent_command_opencode_uses_stdin_and_short_cli_instruction(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_resolve_backend_exe", lambda backend: f"{backend}.exe")
    long_prompt = "PROMPT-LINE-" * 200

    cmd, session_id, stdin_text = orchestrator._agent_command("opencode", long_prompt)

    assert cmd[0] == "opencode.exe"
    assert "run" in cmd
    assert "--format" in cmd
    assert "json" in cmd
    assert "-s" in cmd
    assert "stdin" in cmd[-1].lower()
    assert long_prompt not in " ".join(cmd)
    assert isinstance(session_id, str) and session_id  # cold-start generates UUID
    assert stdin_text == long_prompt


def test_agent_command_opencode_uses_resume_session_when_provided(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_resolve_backend_exe", lambda backend: f"{backend}.exe")

    def fail_uuid4() -> str:
        raise AssertionError("uuid.uuid4 must not be called when resume_session_id is provided")

    monkeypatch.setattr(orchestrator.uuid, "uuid4", fail_uuid4)

    cmd, session_id, stdin_text = orchestrator._agent_command(
        "opencode",
        "opencode prompt payload",
        resume_session_id="sid-reuse-789",
    )

    assert cmd[0] == "opencode.exe"
    assert "run" in cmd
    assert "--format" in cmd
    assert "json" in cmd
    assert "-s" in cmd
    assert cmd[cmd.index("-s") + 1] == "sid-reuse-789"
    assert session_id == "sid-reuse-789"
    assert stdin_text == "opencode prompt payload"


def test_agent_command_unknown_backend_lists_available_backends() -> None:
    with pytest.raises(ValueError) as exc:
        orchestrator._agent_command("unknown-backend", "payload")

    message = str(exc.value)
    assert "Unsupported backend: unknown-backend" in message
    assert "Registered backends:" in message
    assert "claude" in message
    assert "codex" in message


def test_coerce_confidence_bool_values() -> None:
    assert orchestrator._coerce_confidence(True) == 1.0
    assert orchestrator._coerce_confidence(False) == 0.0


def test_main_loop_dir_overrides_all_bus_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    captured: dict[str, Path] = {}

    def fake_status() -> None:
        captured["loop_dir"] = orchestrator.LOOP_DIR
        captured["logs_dir"] = orchestrator.LOGS_DIR
        captured["runtime_dir"] = orchestrator.RUNTIME_DIR
        captured["archive_dir"] = orchestrator.ARCHIVE_DIR
        captured["context_dir"] = orchestrator._CONTEXT_DIR
        captured["module_map_file"] = orchestrator._MODULE_MAP_FILE
        captured["project_facts_file"] = orchestrator._PROJECT_FACTS_FILE
        captured["pitfalls_file"] = orchestrator._PITFALLS_FILE
        captured["patterns_file"] = orchestrator._PATTERNS_FILE
        captured["state_file"] = orchestrator.STATE_FILE
        captured["state_backup"] = orchestrator._STATE_BACKUP
        captured["task_card"] = orchestrator.TASK_CARD
        captured["fix_list"] = orchestrator.FIX_LIST
        captured["work_report"] = orchestrator.WORK_REPORT
        captured["review_req"] = orchestrator.REVIEW_REQ
        captured["review_report"] = orchestrator.REVIEW_REPORT
        captured["lock_file"] = orchestrator.LOCK_FILE

    monkeypatch.setattr(orchestrator, "cmd_status", fake_status)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "status", "--loop-dir", "my-loop"])

    orchestrator.main()

    expected_loop = (tmp_path / "my-loop").resolve()
    assert captured["loop_dir"] == expected_loop
    assert captured["logs_dir"] == expected_loop / "logs"
    assert captured["runtime_dir"] == expected_loop / "runtime"
    assert captured["archive_dir"] == expected_loop / "archive"
    assert captured["context_dir"] == expected_loop / "context"
    assert captured["module_map_file"] == expected_loop / "context" / "module_map.json"
    assert captured["project_facts_file"] == expected_loop / "context" / "project_facts.md"
    assert captured["pitfalls_file"] == expected_loop / "context" / "pitfalls.md"
    assert captured["patterns_file"] == expected_loop / "context" / "patterns.jsonl"
    assert captured["state_file"] == expected_loop / "state.json"
    assert captured["state_backup"] == expected_loop / ".state.json.bak"
    assert captured["task_card"] == expected_loop / "task_card.json"
    assert captured["fix_list"] == expected_loop / "fix_list.json"
    assert captured["work_report"] == expected_loop / "work_report.json"
    assert captured["review_req"] == expected_loop / "review_request.json"
    assert captured["review_report"] == expected_loop / "review_report.json"
    assert captured["lock_file"] == expected_loop / "lock"


def test_main_init_creates_prompt_templates_in_loop_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "init", "--loop-dir", "my-loop"])

    orchestrator.main()

    templates_dir = (tmp_path / "my-loop" / "templates").resolve()
    assert (templates_dir / "worker_prompt.txt").exists()
    assert (templates_dir / "reviewer_prompt.txt").exists()
    worker_template = (templates_dir / "worker_prompt.txt").read_text(encoding="utf-8")
    reviewer_template = (templates_dir / "reviewer_prompt.txt").read_text(encoding="utf-8")
    assert "{knowledge_section}" in worker_template
    assert "{quickstart_section}" in worker_template
    assert "{handoff_section}" in worker_template
    assert "{handoff_section}" in reviewer_template
    assert (tmp_path / "my-loop" / "handoff").exists()
    module_map_path = (tmp_path / "my-loop" / "context" / "module_map.json").resolve()
    module_map = json.loads(module_map_path.read_text(encoding="utf-8"))
    assert module_map["files"] == []
    assert module_map["total_files"] == 0
    project_facts = (tmp_path / "my-loop" / "context" / "project_facts.md").resolve()
    pitfalls = (tmp_path / "my-loop" / "context" / "pitfalls.md").resolve()
    patterns = (tmp_path / "my-loop" / "context" / "patterns.jsonl").resolve()
    assert project_facts.exists()
    assert pitfalls.exists()
    assert patterns.exists()
    assert "single-file rule" in project_facts.read_text(encoding="utf-8")
    assert "lock stale after crash" in pitfalls.read_text(encoding="utf-8")
    pattern_entry = json.loads(patterns.read_text(encoding="utf-8").strip())
    assert pattern_entry["category"] == "example"
    assert pattern_entry["confidence"] == 0.0


def test_main_index_dispatches_to_cmd_index(monkeypatch) -> None:
    called = {"index": False}

    def fake_cmd_index() -> None:
        called["index"] = True

    monkeypatch.setattr(orchestrator, "cmd_index", fake_cmd_index)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "index"])

    orchestrator.main()

    assert called["index"] is True


def test_register_backend_allows_custom_backend_in_run_cli(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_BACKEND_REGISTRY", dict(orchestrator._BACKEND_REGISTRY))

    def resolve_exe(backend: str) -> str:
        return f"{backend}.exe"

    def build_cmd(exe: str, prompt: str) -> tuple[list[str], str | None, str | None]:
        return [exe, "--prompt", prompt], "my-session", None

    orchestrator.register_backend(
        "mybackend",
        build_cmd,
        resolve_exe,
        lambda role, backend, line: None,
    )
    cmd, session_id, stdin_text = orchestrator._agent_command("mybackend", "payload")
    assert cmd == ["mybackend.exe", "--prompt", "payload"]
    assert session_id == "my-session"
    assert stdin_text is None

    captured: dict[str, str] = {}

    def fake_cmd_run(
        config: orchestrator.RunConfig,
        single_round: bool,
        round_num: int | None,
        resume: bool = False,
        reset: bool = False,
    ) -> None:
        _ = (single_round, round_num, resume, reset)
        captured["worker_backend"] = config.worker_backend
        captured["reviewer_backend"] = config.reviewer_backend

    monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orchestrator.py",
            "run",
            "--worker-backend",
            "mybackend",
            "--reviewer-backend",
            "mybackend",
        ],
    )

    orchestrator.main()

    assert captured["worker_backend"] == "mybackend"
    assert captured["reviewer_backend"] == "mybackend"


def test_register_backend_custom_parse_event_fn_is_used_by_stream_dispatch(monkeypatch, capsys) -> None:
    monkeypatch.setattr(orchestrator, "_BACKEND_REGISTRY", dict(orchestrator._BACKEND_REGISTRY))
    monkeypatch.setattr(orchestrator, "_stream_local", threading.local())

    def resolve_exe(backend: str) -> str:
        return f"{backend}.exe"

    def build_cmd(exe: str, prompt: str) -> tuple[list[str], str | None, str | None]:
        return [exe, "--prompt", prompt], "my-session", None

    def parse_event(role: str, backend: str, line: str) -> str | None:
        _ = backend
        if line == "custom event":
            return f"[{role}] Custom summary"
        return None

    orchestrator.register_backend("mybackend", build_cmd, resolve_exe, parse_event)
    parse_event_fn = orchestrator._require_registered_parse_event("mybackend")
    orchestrator._stream_dispatch_stdout_line(
        "worker",
        "mybackend",
        "custom event\n",
        parse_event_fn,
        verbose=False,
    )

    out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert out_lines == ["[worker] Custom summary"]


def test_run_auto_dispatch_passes_stdin_text_to_subprocess(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        captured["stdin"] = kwargs.get("stdin")
        proc = _FakeProc(stdout_lines=['{"type":"thread.started","thread_id":"tid-1"}\n'])
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_popen)

    orchestrator._run_auto_dispatch("worker", "codex", "ignored", 30)

    assert captured["cmd"] == ["codex.exe", "exec", "short instruction"]
    assert captured["cwd"] == str(orchestrator.ROOT)
    assert captured["stdin"] == subprocess.PIPE
    proc = captured["proc"]
    assert isinstance(proc, _FakeProc)
    assert proc.stdin.value == "STDIN_PAYLOAD"
    assert proc.stdin.closed is True


def test_run_auto_dispatch_zero_timeout_is_unlimited(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)

    proc = _FakeProc(stdout_lines=[], poll_ready_after=2)
    monotonic_values = iter([0.0, 10_000.0, 20_000.0])

    def fake_monotonic() -> float:
        return next(monotonic_values, 20_000.0)

    monkeypatch.setattr(orchestrator.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _: None)
    monkeypatch.setattr(orchestrator.subprocess, "Popen", lambda cmd, **kwargs: proc)

    orchestrator._run_auto_dispatch("worker", "codex", "ignored", 0)

    assert proc.terminate_called is False
    assert proc.wait_called is True


def test_run_auto_dispatch_timeout_kills_and_waits_process(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)

    proc = _FakeProc(stdout_lines=[], stderr_lines=["auth failed\n"], poll_ready_after=999_999)

    monotonic_values = iter([0.0, 31.0])

    def fake_monotonic() -> float:
        return next(monotonic_values, 31.0)

    monkeypatch.setattr(orchestrator.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _: None)
    monkeypatch.setattr(orchestrator.subprocess, "Popen", lambda cmd, **kwargs: proc)

    with pytest.raises(orchestrator.DispatchTimeoutError) as exc:
        orchestrator._run_auto_dispatch("worker", "codex", "ignored", 30)

    assert proc.terminate_called is True
    assert proc.wait_called is True
    assert "Backend codex timed out." in str(exc.value)
    assert "(current: 30s)" in str(exc.value)


def test_run_auto_dispatch_timeout_still_triggers_when_stdin_write_blocks(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)

    proc = _FakeProc(stdout_lines=[], stderr_lines=["auth failed\n"], poll_ready_after=999_999)
    proc.stdin = _BlockingStdin()
    monotonic_values = iter([0.0, 31.0])

    def fake_monotonic() -> float:
        return next(monotonic_values, 31.0)

    monkeypatch.setattr(orchestrator.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _: None)
    monkeypatch.setattr(orchestrator.subprocess, "Popen", lambda cmd, **kwargs: proc)

    with pytest.raises(orchestrator.DispatchTimeoutError):
        orchestrator._run_auto_dispatch("worker", "codex", "ignored", 30)

    assert proc.terminate_called is True
    assert proc.wait_called is True
    assert isinstance(proc.stdin, _BlockingStdin)
    assert proc.stdin.closed is True


def test_collect_streamed_text_output_waits_with_timeout_on_stream_exception() -> None:
    proc = _FakeProc(stdout_lines=["line-1\n"], poll_ready_after=999_999)

    def fail_callback(_raw_line: str) -> None:
        raise RuntimeError("stream boom")

    with pytest.raises(RuntimeError, match="stream boom"):
        orchestrator._collect_streamed_text_output(
            proc,
            stdout_line_callback=fail_callback,
        )

    assert proc.wait_called is True
    assert proc.wait_timeouts == [1]


def test_run_auto_dispatch_streams_compact_summaries_in_non_verbose_mode(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(
            stdout_lines=[
                '{"type":"item.completed","item":{"type":"command_execution","command":"git status --short"}}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"Implementing changes..."}}\n',
            ],
        ),
    )

    orchestrator._run_auto_dispatch("worker", "codex", "ignored", 30, verbose=False)

    out = capsys.readouterr().out
    assert "[worker] Running: git status --short" in out
    assert "[worker] Message: Implementing changes..." in out


def test_run_auto_dispatch_verbose_prints_all_stdout_lines(monkeypatch, capsys) -> None:
    raw_lines = [
        '{"type":"item.completed","item":{"type":"agent_message","text":"hello"}}\n',
        "plain status line\n",
    ]
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(stdout_lines=raw_lines),
    )

    orchestrator._run_auto_dispatch("worker", "codex", "ignored", 30, verbose=True)

    out = capsys.readouterr().out
    assert raw_lines[0].strip() not in out
    assert raw_lines[1].strip() not in out
    assert "[worker] Message: hello" in out


def test_run_auto_dispatch_non_verbose_filters_out_non_summary_lines(monkeypatch, capsys) -> None:
    json_line = '{"type":"item.completed","item":{"type":"command_execution","command":"git status"}}\n'
    plain_line = "plain status line\n"
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(stdout_lines=[plain_line, json_line]),
    )

    orchestrator._run_auto_dispatch("worker", "codex", "ignored", 30, verbose=False)

    out = capsys.readouterr().out
    assert "[worker] Running: git status" in out
    assert plain_line.strip() not in out
    assert json_line.strip() not in out


def test_stream_dispatch_stdout_line_uses_codex_registered_parser(monkeypatch, capsys) -> None:
    monkeypatch.setattr(orchestrator, "_stream_local", threading.local())
    parse_event_fn = orchestrator._require_registered_parse_event("codex")
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "git status --short"},
        }
    )

    orchestrator._stream_dispatch_stdout_line(
        "worker",
        "codex",
        line + "\n",
        parse_event_fn,
        verbose=False,
    )

    out_lines = [item.strip() for item in capsys.readouterr().out.splitlines() if item.strip()]
    assert out_lines == ["[worker] Running: git status --short"]


def test_stream_dispatch_stdout_line_claude_parser_stub_suppresses_summaries(monkeypatch, capsys) -> None:
    monkeypatch.setattr(orchestrator, "_stream_local", threading.local())
    parse_event_fn = orchestrator._require_registered_parse_event("claude")
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "git status --short"},
        }
    )

    orchestrator._stream_dispatch_stdout_line(
        "worker",
        "claude",
        line + "\n",
        parse_event_fn,
        verbose=False,
    )

    assert capsys.readouterr().out == ""


def test_stream_dispatch_stdout_line_collapses_consecutive_reads(monkeypatch, capsys) -> None:
    monkeypatch.setattr(orchestrator, "_stream_local", threading.local())
    line_1 = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "Get-Content -Path src/loop_kit/orchestrator.py | Select-Object -Skip 0 -First 40",
            },
        }
    )
    line_2 = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "cat src/loop_kit/orchestrator.py | Select-Object -Skip 40 -First 40",
            },
        }
    )

    orchestrator._stream_dispatch_stdout_line(
        "reader",
        "codex",
        line_1 + "\n",
        orchestrator._codex_event_summary,
        verbose=False,
    )
    orchestrator._stream_dispatch_stdout_line(
        "reader",
        "codex",
        line_2 + "\n",
        orchestrator._codex_event_summary,
        verbose=False,
    )

    out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert out_lines == ["[reader] Reading: orchestrator.py"]


def test_stream_dispatch_stdout_line_read_collapse_resets_on_non_summary_line(monkeypatch, capsys) -> None:
    monkeypatch.setattr(orchestrator, "_stream_local", threading.local())
    read_line = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "Get-Content src/loop_kit/orchestrator.py | Select-Object -Skip 0 -First 40",
            },
        }
    )

    orchestrator._stream_dispatch_stdout_line(
        "reader",
        "codex",
        read_line + "\n",
        orchestrator._codex_event_summary,
        verbose=False,
    )
    orchestrator._stream_dispatch_stdout_line(
        "reader",
        "codex",
        "plain status line\n",
        orchestrator._codex_event_summary,
        verbose=False,
    )
    orchestrator._stream_dispatch_stdout_line(
        "reader",
        "codex",
        read_line + "\n",
        orchestrator._codex_event_summary,
        verbose=False,
    )

    out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert out_lines == [
        "[reader] Reading: orchestrator.py",
        "[reader] Reading: orchestrator.py",
    ]


def test_run_auto_dispatch_dispatch_log_keeps_full_stdout_when_non_verbose(tmp_path: Path, monkeypatch, capsys) -> None:
    raw_lines = [
        "plain status line\n",
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
    ]
    monkeypatch.setattr(orchestrator, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(stdout_lines=raw_lines),
    )

    orchestrator._run_auto_dispatch("worker", "codex", "ignored", 30, verbose=False)

    _ = capsys.readouterr()
    dispatch_log = (tmp_path / "worker_dispatch.log").read_text(encoding="utf-8")
    assert raw_lines[0].strip() in dispatch_log
    assert raw_lines[1].strip() in dispatch_log


def test_run_auto_dispatch_does_not_collapse_reads_across_dispatch_runs(monkeypatch, capsys) -> None:
    monkeypatch.setattr(orchestrator, "_stream_local", threading.local())
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)

    read_line_1 = (
        '{"type":"item.completed","item":{"type":"command_execution",'
        '"command":"Get-Content src/loop_kit/orchestrator.py | Select-Object -Skip 0 -First 40"}}\n'
    )
    read_line_2 = (
        '{"type":"item.completed","item":{"type":"command_execution",'
        '"command":"Get-Content src/loop_kit/orchestrator.py | Select-Object -Skip 40 -First 40"}}\n'
    )
    attempts = [
        _FakeProc(stdout_lines=[read_line_1]),
        _FakeProc(stdout_lines=[read_line_2]),
    ]

    def fake_popen(cmd, **kwargs):
        _ = (cmd, kwargs)
        return attempts.pop(0)

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_popen)

    orchestrator._run_auto_dispatch("worker", "codex", "ignored", 30, verbose=False)
    orchestrator._run_auto_dispatch("worker", "codex", "ignored", 30, verbose=False)

    out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert out_lines == [
        "[worker] Reading: orchestrator.py",
        "[worker] Reading: orchestrator.py",
    ]


def test_run_auto_dispatch_retries_and_succeeds_on_second_attempt(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)

    attempts: list[_FakeProc] = [
        _FakeProc(stdout_lines=[], stderr_lines=["first fail\n"], returncode=1),
        _FakeProc(stdout_lines=['{"type":"thread.started","thread_id":"tid-2"}\n'], returncode=0),
    ]
    popen_calls: list[list[str]] = []
    sleep_calls: list[int] = []

    def fake_popen(cmd, **kwargs):
        _ = kwargs
        popen_calls.append(cmd)
        return attempts[len(popen_calls) - 1]

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda sec: sleep_calls.append(sec))

    orchestrator._run_auto_dispatch(
        "worker",
        "codex",
        "ignored",
        30,
        dispatch_retries=2,
        dispatch_retry_base_sec=5,
    )

    assert len(popen_calls) == 2
    assert sleep_calls == [5]


def test_run_auto_dispatch_retry_exhaustion_raises_final_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)

    popen_calls: list[list[str]] = []
    sleep_calls: list[int] = []

    def fake_popen(cmd, **kwargs):
        _ = kwargs
        popen_calls.append(cmd)
        return _FakeProc(stdout_lines=[], stderr_lines=["still failing\n"], returncode=3)

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda sec: sleep_calls.append(sec))

    with pytest.raises(RuntimeError) as exc:
        orchestrator._run_auto_dispatch(
            "worker",
            "codex",
            "ignored",
            30,
            dispatch_retries=2,
            dispatch_retry_base_sec=5,
        )

    assert "after 3 attempts" in str(exc.value)
    assert "(backend=codex, rc=3)" in str(exc.value)
    assert len(popen_calls) == 3
    assert sleep_calls == [5, 10]


def test_run_auto_dispatch_permanent_error_fails_fast_without_retrying(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)

    popen_calls: list[list[str]] = []
    sleep_calls: list[int] = []

    def fake_popen(cmd, **kwargs):
        _ = kwargs
        popen_calls.append(cmd)
        return _FakeProc(stdout_lines=[], stderr_lines=["authentication failed\n"], returncode=1)

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda sec: sleep_calls.append(sec))

    with pytest.raises(RuntimeError) as exc:
        orchestrator._run_auto_dispatch(
            "worker",
            "codex",
            "ignored",
            30,
            dispatch_retries=2,
            dispatch_retry_base_sec=5,
        )

    assert "permanent error, not retrying" in str(exc.value)
    assert "authentication failed" in str(exc.value)
    assert "(backend=codex, rc=1)" in str(exc.value)
    assert len(popen_calls) == 1
    assert sleep_calls == []


def test_run_auto_dispatch_heartbeat_writer_writes_expected_payload(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    stop_event = _FakeEvent()

    orchestrator._run_auto_dispatch_heartbeat_writer(
        role="worker",
        stop_event=stop_event,
        interval_sec=18.0,
        task_id="T-617",
        round_num=3,
    )

    heartbeat_data = json.loads(orchestrator._heartbeat_path("worker").read_text(encoding="utf-8"))
    assert heartbeat_data["task_id"] == "T-617"
    assert heartbeat_data["round_num"] == 3
    assert heartbeat_data["role"] == "worker"
    assert isinstance(heartbeat_data["timestamp"], str)
    assert stop_event.wait_calls == [18.0]


def test_stop_auto_dispatch_heartbeat_sets_event_and_joins_thread(monkeypatch) -> None:
    stop_event = _FakeEvent()
    thread = _FakeThread(daemon=True)
    monkeypatch.setattr(orchestrator, "_AUTO_DISPATCH_HEARTBEATS", {"worker": (stop_event, thread)})

    orchestrator._stop_auto_dispatch_heartbeat("worker")

    assert stop_event.set_called is True
    assert thread.join_timeouts == [orchestrator._AUTO_DISPATCH_HEARTBEAT_JOIN_TIMEOUT_SEC]
    assert orchestrator._AUTO_DISPATCH_HEARTBEATS == {}


def test_start_auto_dispatch_heartbeat_replaces_existing_thread_and_sets_daemon(monkeypatch) -> None:
    old_event = _FakeEvent()
    old_thread = _FakeThread(daemon=True)
    new_event = _FakeEvent()
    created_threads: list[_FakeThread] = []
    monkeypatch.setattr(orchestrator, "_AUTO_DISPATCH_HEARTBEATS", {"worker": (old_event, old_thread)})
    monkeypatch.setattr(orchestrator.threading, "Event", lambda: new_event)

    def fake_thread_ctor(*args, **kwargs):
        _ = args
        thread = _FakeThread(
            target=kwargs.get("target"),
            args=kwargs.get("args", ()),
            daemon=kwargs.get("daemon"),
            name=kwargs.get("name"),
        )
        created_threads.append(thread)
        return thread

    monkeypatch.setattr(orchestrator.threading, "Thread", fake_thread_ctor)

    orchestrator._start_auto_dispatch_heartbeat(
        "worker",
        heartbeat_ttl_sec=40,
        task_id="T-617",
        round_num=1,
    )

    assert old_event.set_called is True
    assert old_thread.join_timeouts == [orchestrator._AUTO_DISPATCH_HEARTBEAT_JOIN_TIMEOUT_SEC]
    assert len(created_threads) == 1
    created = created_threads[0]
    assert created.daemon is True
    assert created.started is True
    assert created.args[0] == "worker"
    assert created.args[1] is new_event
    assert created.args[2] == pytest.approx(20.0)
    assert created.args[3] == "T-617"
    assert created.args[4] == 1
    assert orchestrator._AUTO_DISPATCH_HEARTBEATS["worker"] == (new_event, created)


def test_run_auto_dispatch_heartbeat_lifecycle_started_and_stopped(monkeypatch) -> None:
    start_calls: list[tuple[str, dict[str, object]]] = []
    stop_calls: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_start_auto_dispatch_heartbeat",
        lambda role, **kwargs: start_calls.append((role, kwargs)),
    )
    monkeypatch.setattr(
        orchestrator,
        "_stop_auto_dispatch_heartbeat",
        lambda role: stop_calls.append(role),
    )
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(stdout_lines=['{"type":"thread.started","thread_id":"tid-1"}\n']),
    )

    orchestrator._run_auto_dispatch(
        "worker",
        "codex",
        "ignored",
        30,
        heartbeat_enabled=True,
        heartbeat_ttl_sec=44,
        task_id="T-617",
        round_num=2,
    )

    assert start_calls == [
        (
            "worker",
            {
                "heartbeat_ttl_sec": 44,
                "task_id": "T-617",
                "round_num": 2,
            },
        )
    ]
    assert stop_calls == ["worker"]


def test_run_auto_dispatch_heartbeat_stops_on_failure(monkeypatch) -> None:
    stop_calls: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_feed_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_start_auto_dispatch_heartbeat", lambda role, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_stop_auto_dispatch_heartbeat", lambda role: stop_calls.append(role))
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(stdout_lines=[], stderr_lines=["authentication failed\n"], returncode=1),
    )

    with pytest.raises(RuntimeError, match="permanent error, not retrying"):
        orchestrator._run_auto_dispatch(
            "worker",
            "codex",
            "ignored",
            30,
            dispatch_retries=2,
            dispatch_retry_base_sec=5,
            heartbeat_enabled=True,
            task_id="T-617",
            round_num=2,
        )

    assert stop_calls == ["worker"]


def test_auto_dispatch_role_only_enables_heartbeat_when_required(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    def fake_run_auto_dispatch(*, heartbeat_enabled: bool, **kwargs) -> None:
        _ = kwargs
        captured.append({"heartbeat_enabled": heartbeat_enabled})

    def fake_dispatch_with_artifact_fallback(
        *,
        role: str,
        dispatch_call,
        artifact_path: Path,
        task_id: str,
        round_num: int,
        timeout_sec: int = orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
    ) -> dict:
        _ = (role, artifact_path, task_id, round_num, timeout_sec)
        dispatch_call()
        return {"task_id": task_id, "round": round_num}

    monkeypatch.setattr(orchestrator, "_run_auto_dispatch", fake_run_auto_dispatch)
    monkeypatch.setattr(orchestrator, "_dispatch_with_artifact_fallback", fake_dispatch_with_artifact_fallback)
    monkeypatch.setattr(
        orchestrator, "_load_state", lambda: {"state": "idle", "round": 0, "task_id": None, "sessions": {}}
    )
    monkeypatch.setattr(orchestrator, "_save_state", lambda state: None)

    config_with_hb = orchestrator.RunConfig(
        auto_dispatch=True,
        require_heartbeat=True,
    )
    config_without_hb = orchestrator.RunConfig(
        auto_dispatch=True,
        require_heartbeat=False,
    )

    orchestrator._auto_dispatch_role(
        role="worker",
        prompt="prompt",
        config=config_with_hb,
        task_id="T-617",
        round_num=1,
        artifact_path=Path("unused.json"),
    )
    orchestrator._auto_dispatch_role(
        role="worker",
        prompt="prompt",
        config=config_without_hb,
        task_id="T-617",
        round_num=1,
        artifact_path=Path("unused.json"),
    )

    assert captured == [
        {"heartbeat_enabled": True},
        {"heartbeat_enabled": False},
    ]


class TestSessionManager:
    def test_normalize_session_id(self) -> None:
        assert orchestrator.SessionManager.normalize_session_id(None) is None
        assert orchestrator.SessionManager.normalize_session_id("") is None
        assert orchestrator.SessionManager.normalize_session_id("  ") is None
        assert orchestrator.SessionManager.normalize_session_id(" sid-1 ") == "sid-1"

    def test_store_and_get_session(self) -> None:
        manager = orchestrator.SessionManager(role="worker")
        state: dict[str, object] = {}

        changed = manager.store_session(state, " CoDeX ", " sid-1 ", round_num=2)

        assert changed is True
        assert manager.get_session(state, "codex") == "sid-1"
        assert state["sessions"] == {
            "worker": {"session_id": "sid-1", "backend": "codex", "started_round": 2}
        }

    def test_store_session_preserves_started_round_when_session_unchanged(self) -> None:
        manager = orchestrator.SessionManager(role="worker")
        state = {
            "sessions": {"worker": {"session_id": "sid-1", "backend": "codex", "started_round": 2}},
        }

        changed = manager.store_session(state, "codex", "sid-1", round_num=4)

        assert changed is False
        assert state["sessions"] == {
            "worker": {"session_id": "sid-1", "backend": "codex", "started_round": 2}
        }

    def test_get_session_returns_none_on_backend_mismatch(self) -> None:
        manager = orchestrator.SessionManager(role="worker")
        state = {"sessions": {"worker": {"session_id": "sid-1", "backend": "codex", "started_round": 2}}}

        assert manager.get_session(state, "opencode") is None

    def test_invalidate_session(self) -> None:
        manager = orchestrator.SessionManager(role="worker")
        state = {"sessions": {"worker": {"session_id": "sid-1", "backend": "codex", "started_round": 2}}}

        assert manager.invalidate_session(state, "opencode") is False
        assert manager.invalidate_session(state, "codex") is True
        assert state["sessions"] == {}

    def test_build_resume_context(self) -> None:
        manager = orchestrator.SessionManager(role="reviewer")
        state = {"sessions": {"reviewer": {"session_id": "sid-r", "backend": "claude", "started_round": 1}}}

        assert manager.build_resume_context(state, "claude") == "sid-r"
        assert manager.build_resume_context(state, "codex") is None


def test_auto_dispatch_role_reuses_and_persists_worker_session(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    state = {
        "state": orchestrator.STATE_AWAITING_WORK,
        "round": 2,
        "task_id": "T-627",
        "base_sha": "base-sha",
        "sessions": {"worker": {"session_id": "sid-old", "backend": "codex"}},
    }
    orchestrator._save_state(state)

    captured: dict[str, object] = {}

    def fake_run_auto_dispatch(*, resume_session_id: str | None = None, **kwargs) -> str | None:
        _ = kwargs
        captured["resume_session_id"] = resume_session_id
        return "sid-new"

    def fake_dispatch_with_artifact_fallback(
        *,
        role: str,
        dispatch_call,
        artifact_path: Path,
        task_id: str,
        round_num: int,
        timeout_sec: int = orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
    ) -> dict:
        _ = (role, artifact_path, timeout_sec)
        dispatch_call()
        return {"task_id": task_id, "round": round_num}

    monkeypatch.setattr(orchestrator, "_run_auto_dispatch", fake_run_auto_dispatch)
    monkeypatch.setattr(orchestrator, "_dispatch_with_artifact_fallback", fake_dispatch_with_artifact_fallback)
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")

    result = orchestrator._auto_dispatch_role(
        role="worker",
        prompt="prompt",
        config=orchestrator.RunConfig(auto_dispatch=True, worker_backend="codex"),
        task_id="T-627",
        round_num=2,
        artifact_path=orchestrator.WORK_REPORT,
        state=state,
    )

    assert result == {"task_id": "T-627", "round": 2}
    assert captured["resume_session_id"] == "sid-old"
    saved = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
    assert saved["sessions"]["worker"] == {"session_id": "sid-new", "backend": "codex", "started_round": 2}


def test_auto_dispatch_role_reuses_and_persists_worker_session_opencode(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    state = {
        "state": orchestrator.STATE_AWAITING_WORK,
        "round": 2,
        "task_id": "T-628",
        "base_sha": "base-sha",
        "sessions": {"worker": {"session_id": "sid-opencode-old", "backend": "opencode"}},
    }
    orchestrator._save_state(state)

    captured: dict[str, object] = {}

    def fake_run_auto_dispatch(*, resume_session_id: str | None = None, **kwargs) -> str | None:
        _ = kwargs
        captured["resume_session_id"] = resume_session_id
        return "sid-opencode-new"

    def fake_dispatch_with_artifact_fallback(
        *,
        role: str,
        dispatch_call,
        artifact_path: Path,
        task_id: str,
        round_num: int,
        timeout_sec: int = orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
    ) -> dict:
        _ = (role, artifact_path, timeout_sec)
        dispatch_call()
        return {"task_id": task_id, "round": round_num}

    monkeypatch.setattr(orchestrator, "_run_auto_dispatch", fake_run_auto_dispatch)
    monkeypatch.setattr(orchestrator, "_dispatch_with_artifact_fallback", fake_dispatch_with_artifact_fallback)
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")

    result = orchestrator._auto_dispatch_role(
        role="worker",
        prompt="prompt",
        config=orchestrator.RunConfig(auto_dispatch=True, worker_backend="opencode"),
        task_id="T-628",
        round_num=2,
        artifact_path=orchestrator.WORK_REPORT,
        state=state,
    )

    assert result == {"task_id": "T-628", "round": 2}
    assert captured["resume_session_id"] == "sid-opencode-old"
    saved = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
    assert saved["sessions"]["worker"] == {
        "session_id": "sid-opencode-new",
        "backend": "opencode",
        "started_round": 2,
    }


def test_auto_dispatch_role_invalidates_sessions_on_base_sha_mismatch(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    state = {
        "state": orchestrator.STATE_AWAITING_WORK,
        "round": 2,
        "task_id": "T-627",
        "base_sha": "base-sha",
        "sessions": {"worker": {"session_id": "sid-old", "backend": "codex"}},
    }
    orchestrator._save_state(state)

    captured: dict[str, object] = {}

    def fake_run_auto_dispatch(*, resume_session_id: str | None = None, **kwargs) -> str | None:
        _ = kwargs
        captured["resume_session_id"] = resume_session_id
        return None

    def fake_dispatch_with_artifact_fallback(
        *,
        role: str,
        dispatch_call,
        artifact_path: Path,
        task_id: str,
        round_num: int,
        timeout_sec: int = orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
    ) -> dict:
        _ = (role, artifact_path, timeout_sec)
        dispatch_call()
        return {"task_id": task_id, "round": round_num}

    monkeypatch.setattr(orchestrator, "_run_auto_dispatch", fake_run_auto_dispatch)
    monkeypatch.setattr(orchestrator, "_dispatch_with_artifact_fallback", fake_dispatch_with_artifact_fallback)
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "new-head-sha")

    orchestrator._auto_dispatch_role(
        role="worker",
        prompt="prompt",
        config=orchestrator.RunConfig(auto_dispatch=True, worker_backend="codex"),
        task_id="T-627",
        round_num=2,
        artifact_path=orchestrator.WORK_REPORT,
        state=state,
    )

    assert captured["resume_session_id"] is None
    saved = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
    assert saved["sessions"] == {}


def test_auto_dispatch_role_rotates_session_and_emits_artifact_metric(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    state = {
        "state": orchestrator.STATE_AWAITING_WORK,
        "round": 3,
        "task_id": "T-627",
        "base_sha": "base-sha",
        "sessions": {"worker": {"session_id": "sid-old", "backend": "codex", "started_round": 1}},
    }
    orchestrator._save_state(state)

    captured: dict[str, object] = {}
    events: list[tuple[str, dict[str, object]]] = []

    def fake_run_auto_dispatch(*, resume_session_id: str | None = None, **kwargs) -> str | None:
        _ = kwargs
        captured["resume_session_id"] = resume_session_id
        return "sid-new"

    def fake_dispatch_with_artifact_fallback(
        *,
        role: str,
        dispatch_call,
        artifact_path: Path,
        task_id: str,
        round_num: int,
        timeout_sec: int = orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
    ) -> dict:
        _ = (role, artifact_path, timeout_sec)
        dispatch_call()
        return {"task_id": task_id, "round": round_num}

    def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
        _ = level
        events.append((event, dict(data or {})))

    monkeypatch.setattr(orchestrator, "_run_auto_dispatch", fake_run_auto_dispatch)
    monkeypatch.setattr(orchestrator, "_dispatch_with_artifact_fallback", fake_dispatch_with_artifact_fallback)
    monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")

    result = orchestrator._auto_dispatch_role(
        role="worker",
        prompt="prompt",
        config=orchestrator.RunConfig(auto_dispatch=True, worker_backend="codex", max_session_rounds=2),
        task_id="T-627",
        round_num=3,
        artifact_path=orchestrator.WORK_REPORT,
        state=state,
    )

    assert result == {"task_id": "T-627", "round": 3}
    assert captured["resume_session_id"] is None
    saved = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
    assert saved["sessions"]["worker"]["session_id"] == "sid-new"
    assert saved["sessions"]["worker"]["started_round"] == 3
    resume_events = [payload for event, payload in events if event == orchestrator.FEED_DISPATCH_RESUME]
    assert resume_events
    assert resume_events[0]["status"] == "resume_rotated"
    artifact_events = [payload for event, payload in events if event == orchestrator.FEED_DISPATCH_ARTIFACT_WRITTEN]
    assert artifact_events
    assert artifact_events[0]["artifact_path"] == "work_report.json"
    assert isinstance(artifact_events[0]["latency_ms"], int)


def test_auto_dispatch_role_emits_dispatch_phase_metrics_with_complete_boundaries(monkeypatch) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    state = {
        "state": orchestrator.STATE_AWAITING_WORK,
        "round": 3,
        "task_id": "T-715",
        "sessions": {},
    }
    monotonic_values = iter([10.0, 11.0])

    def fake_run_auto_dispatch(*, telemetry: dict[str, object] | None = None, **kwargs) -> str | None:
        _ = kwargs
        if telemetry is not None:
            telemetry["first_stdout_ms"] = 120
            telemetry["first_work_action_ms"] = 420
            telemetry["first_meaningful_action_ms"] = 380
            telemetry["subphase_ms"] = {"read": 80, "search": 120, "edit": 150, "test": 50, "unknown": 0}
            telemetry["subphase_counts"] = {"read": 1, "search": 1, "edit": 1, "test": 1, "unknown": 0}
            telemetry["active_subphase"] = "edit"
            telemetry["active_subphase_started_ms"] = 900
        return "sid-metric"

    def fake_dispatch_with_artifact_fallback(
        *,
        role: str,
        dispatch_call,
        artifact_path: Path,
        task_id: str,
        round_num: int,
        timeout_sec: int = orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
    ) -> dict:
        _ = (role, artifact_path, timeout_sec)
        dispatch_call()
        return {"task_id": task_id, "round": round_num}

    def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
        _ = level
        events.append((event, dict(data or {})))

    monkeypatch.setattr(orchestrator, "_run_auto_dispatch", fake_run_auto_dispatch)
    monkeypatch.setattr(orchestrator, "_dispatch_with_artifact_fallback", fake_dispatch_with_artifact_fallback)
    monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: next(monotonic_values, 11.0))
    monkeypatch.setattr(orchestrator, "_save_state", lambda state_data: None)

    result = orchestrator._auto_dispatch_role(
        role="worker",
        prompt="prompt",
        config=orchestrator.RunConfig(auto_dispatch=True, worker_backend="codex"),
        task_id="T-715",
        round_num=2,
        artifact_path=orchestrator.WORK_REPORT,
        state=state,
    )

    assert result == {"task_id": "T-715", "round": 2}
    phase_events = [payload for event, payload in events if event == orchestrator.FEED_DISPATCH_PHASE_METRICS]
    assert len(phase_events) == 1
    payload = phase_events[0]
    assert payload["startup_ms"] == 120
    assert payload["context_to_work_ms"] == 300
    assert payload["work_to_artifact_ms"] == 580
    assert payload["total_ms"] == 1000
    assert payload["read_ms"] == 80
    assert payload["search_ms"] == 120
    assert payload["edit_ms"] == 250
    assert payload["test_ms"] == 50
    assert payload["unknown_ms"] is None
    assert payload["read_count"] == 1
    assert payload["search_count"] == 1
    assert payload["edit_count"] == 1
    assert payload["test_count"] == 1
    assert payload["unknown_count"] == 0
    assert payload["session_id"] == "sid-metric"
    assert payload["task_id"] == "T-715"
    assert payload["round"] == 2
    assert payload["role"] == "worker"


def test_auto_dispatch_role_phase_metrics_graceful_when_work_boundary_missing(monkeypatch) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    state = {
        "state": orchestrator.STATE_AWAITING_WORK,
        "round": 1,
        "task_id": "T-715",
        "sessions": {},
    }
    monotonic_values = iter([2.0, 2.9])

    def fake_run_auto_dispatch(*, telemetry: dict[str, object] | None = None, **kwargs) -> str | None:
        _ = kwargs
        if telemetry is not None:
            telemetry["first_stdout_ms"] = 200
            telemetry["first_work_action_ms"] = None
        return "sid-missing"

    def fake_dispatch_with_artifact_fallback(
        *,
        role: str,
        dispatch_call,
        artifact_path: Path,
        task_id: str,
        round_num: int,
        timeout_sec: int = orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
    ) -> dict:
        _ = (role, artifact_path, timeout_sec)
        dispatch_call()
        return {"task_id": task_id, "round": round_num}

    def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
        _ = level
        events.append((event, dict(data or {})))

    monkeypatch.setattr(orchestrator, "_run_auto_dispatch", fake_run_auto_dispatch)
    monkeypatch.setattr(orchestrator, "_dispatch_with_artifact_fallback", fake_dispatch_with_artifact_fallback)
    monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: next(monotonic_values, 2.9))
    monkeypatch.setattr(orchestrator, "_save_state", lambda state_data: None)

    orchestrator._auto_dispatch_role(
        role="worker",
        prompt="prompt",
        config=orchestrator.RunConfig(auto_dispatch=True, worker_backend="codex"),
        task_id="T-715",
        round_num=1,
        artifact_path=orchestrator.WORK_REPORT,
        state=state,
    )

    phase_events = [payload for event, payload in events if event == orchestrator.FEED_DISPATCH_PHASE_METRICS]
    assert len(phase_events) == 1
    payload = phase_events[0]
    assert payload["startup_ms"] == 200
    assert payload["context_to_work_ms"] is None
    assert payload["work_to_artifact_ms"] is None
    assert payload["total_ms"] == 899
    assert payload["read_ms"] is None
    assert payload["search_ms"] is None
    assert payload["edit_ms"] is None
    assert payload["test_ms"] is None
    assert payload["unknown_ms"] is None
    assert payload["read_count"] == 0
    assert payload["search_count"] == 0
    assert payload["edit_count"] == 0
    assert payload["test_count"] == 0
    assert payload["unknown_count"] == 0


def test_auto_dispatch_role_dispatch_event_ordering_includes_artifact_boundary(monkeypatch) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    state = {
        "state": orchestrator.STATE_AWAITING_WORK,
        "round": 1,
        "task_id": "T-715",
        "sessions": {},
    }
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt, resume_session_id=None: (
            ["codex.exe", "exec", "short instruction"],
            resume_session_id,
            "STDIN_PAYLOAD",
        ),
    )
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(
            stdout_lines=[
                '{"type":"thread.started","thread_id":"tid-order"}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"I am analyzing"}}\n',
                '{"type":"item.started","item":{"type":"command_execution","command":"git status --short"}}\n',
            ],
            returncode=0,
        ),
    )

    def fake_dispatch_with_artifact_fallback(
        *,
        role: str,
        dispatch_call,
        artifact_path: Path,
        task_id: str,
        round_num: int,
        timeout_sec: int = orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
    ) -> dict:
        _ = (role, artifact_path, timeout_sec)
        dispatch_call()
        return {"task_id": task_id, "round": round_num}

    def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
        _ = level
        events.append((event, dict(data or {})))

    monkeypatch.setattr(orchestrator, "_dispatch_with_artifact_fallback", fake_dispatch_with_artifact_fallback)
    monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)
    monkeypatch.setattr(orchestrator, "_save_state", lambda state_data: None)

    orchestrator._auto_dispatch_role(
        role="worker",
        prompt="prompt",
        config=orchestrator.RunConfig(auto_dispatch=True, worker_backend="codex", dispatch_retries=0),
        task_id="T-715",
        round_num=1,
        artifact_path=orchestrator.WORK_REPORT,
        state=state,
    )

    def _first_index(event_name: str) -> int:
        return next(idx for idx, (event, _payload) in enumerate(events) if event == event_name)

    assert _first_index(orchestrator.FEED_DISPATCH_START) <= _first_index(orchestrator.FEED_DISPATCH_FIRST_STDOUT)
    assert _first_index(orchestrator.FEED_DISPATCH_FIRST_STDOUT) <= _first_index(
        orchestrator.FEED_DISPATCH_FIRST_WORK_ACTION
    )
    assert _first_index(orchestrator.FEED_DISPATCH_FIRST_WORK_ACTION) <= _first_index(
        orchestrator.FEED_DISPATCH_ARTIFACT_WRITTEN
    )


def test_auto_dispatch_role_clears_sessions_on_permanent_dispatch_error(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    state = {
        "state": orchestrator.STATE_AWAITING_WORK,
        "round": 2,
        "task_id": "T-627",
        "base_sha": "base-sha",
        "sessions": {"worker": {"session_id": "sid-old", "backend": "codex"}},
    }
    orchestrator._save_state(state)

    def fake_run_auto_dispatch(**kwargs) -> str | None:
        _ = kwargs
        raise orchestrator.PermanentDispatchError("permanent error")

    def fake_dispatch_with_artifact_fallback(
        *,
        role: str,
        dispatch_call,
        artifact_path: Path,
        task_id: str,
        round_num: int,
        timeout_sec: int = orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
    ) -> dict:
        _ = (role, artifact_path, task_id, round_num, timeout_sec)
        dispatch_call()
        raise AssertionError("dispatch_call should have raised PermanentDispatchError")

    monkeypatch.setattr(orchestrator, "_run_auto_dispatch", fake_run_auto_dispatch)
    monkeypatch.setattr(orchestrator, "_dispatch_with_artifact_fallback", fake_dispatch_with_artifact_fallback)
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")

    with pytest.raises(orchestrator.PermanentDispatchError, match="permanent error"):
        orchestrator._auto_dispatch_role(
            role="worker",
            prompt="prompt",
            config=orchestrator.RunConfig(auto_dispatch=True, worker_backend="codex"),
            task_id="T-627",
            round_num=2,
            artifact_path=orchestrator.WORK_REPORT,
            state=state,
        )

    saved = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
    assert saved["sessions"] == {}


def test_worker_prompt_round1_includes_task_card_section(monkeypatch) -> None:
    def fake_read(path: Path) -> str | None:
        if path.name == "AGENTS.md":
            return "AGENTS_CONTENT"
        if path.name == "code-writer.md":
            return "CODE_WRITER_CONTENT"
        if path == orchestrator._worker_prompt_template_path():
            return None
        return None

    def fake_read_json(path: Path) -> dict | None:
        _ = path
        return {
            "goal": "Improve prompt payload",
            "in_scope": ["item-a"],
            "out_of_scope": ["item-b"],
            "acceptance_criteria": ["item-c"],
            "constraints": ["item-d"],
        }

    monkeypatch.setattr(orchestrator, "_read_text_optional", fake_read)
    monkeypatch.setattr(orchestrator, "_read_json_if_exists", fake_read_json)

    prompt = orchestrator._worker_prompt("T-603", 1)

    assert "AGENTS_CONTENT" in prompt
    assert "CODE_WRITER_CONTENT" in prompt
    assert "Current task_id: T-603, round: 1." in prompt
    assert "=== TASK CARD ===" in prompt
    assert "goal: Improve prompt payload" in prompt
    assert "in_scope:" in prompt
    assert "- item-a" in prompt


def test_worker_prompt_round1_includes_quickstart_context(monkeypatch) -> None:
    def fake_read(path: Path) -> str | None:
        if path.name == "AGENTS.md":
            return "AGENTS_CONTENT"
        if path.name == "code-writer.md":
            return "CODE_WRITER_CONTENT"
        if path == orchestrator._worker_prompt_template_path():
            return None
        return None

    def fake_read_json(path: Path) -> dict | None:
        _ = path
        return {
            "goal": "Quickstart payload",
            "in_scope": [],
            "out_of_scope": [],
            "acceptance_criteria": [],
            "constraints": ["state contract"],
        }

    monkeypatch.setattr(orchestrator, "_read_text_optional", fake_read)
    monkeypatch.setattr(orchestrator, "_read_json_if_exists", fake_read_json)

    prompt = orchestrator._worker_prompt("T-603", 1)

    assert "=== QUICKSTART CONTEXT ===" in prompt
    assert "single-file orchestrator architecture" in prompt
    assert "state.json is the single source of truth" in prompt
    assert "- state contract" in prompt


def test_worker_prompt_round2_includes_prior_round_context(monkeypatch) -> None:
    def fake_read(path: Path) -> str | None:
        if path.name == "AGENTS.md":
            return "AGENTS_CONTENT"
        if path.name == "code-writer.md":
            return "CODE_WRITER_CONTENT"
        return None

    def fake_read_json(path: Path) -> dict | None:
        if path == orchestrator.TASK_CARD:
            return {
                "goal": "Improve prompt payload",
                "in_scope": [],
                "out_of_scope": [],
                "acceptance_criteria": [],
                "constraints": [],
            }
        if path == orchestrator.WORK_REPORT:
            return {"notes": "previous notes", "files_changed": ["tools/orchestrator.py"]}
        if path == orchestrator.REVIEW_REPORT:
            return {
                "blocking_issues": [{"severity": "high", "file": "tools/orchestrator.py", "reason": "fix me"}],
                "non_blocking_suggestions": ["suggestion-1"],
            }
        return None

    monkeypatch.setattr(orchestrator, "_read_text_optional", fake_read)
    monkeypatch.setattr(orchestrator, "_read_json_if_exists", fake_read_json)

    prompt = orchestrator._worker_prompt("T-603", 2)

    assert "=== PRIOR ROUND CONTEXT ===" in prompt
    assert "prior_round_notes: previous notes" in prompt
    assert "prior_round_files_changed:" in prompt
    assert "- tools/orchestrator.py" in prompt
    assert "prior_review_non_blocking:" in prompt
    assert "- suggestion-1" in prompt


def test_worker_prompt_round2_includes_handoff_context_when_available(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    task_id = "T-603"
    handoff_dir = tmp_path / ".loop" / "handoff" / task_id
    handoff_dir.mkdir(parents=True, exist_ok=True)
    (handoff_dir / "worker_r1.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "role": "worker",
                "round": 1,
                "done": ["completed baseline changes"],
                "open_questions": [],
                "next_actions": ["review diff"],
                "evidence": ["head_sha=abc123"],
                "must_read_files": ["src/loop_kit/orchestrator.py"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    prompt = orchestrator._worker_prompt(task_id, 2)

    assert "=== HANDOFF CONTEXT ===" in prompt
    assert "role: worker" in prompt
    assert "completed baseline changes" in prompt
    assert "src/loop_kit/orchestrator.py" in prompt


def test_worker_prompt_round2_skips_agents_md_and_function_index(monkeypatch) -> None:
    read_calls: list[str] = []

    def fake_read(path: Path) -> str | None:
        read_calls.append(path.name if path else str(path))
        if "code-writer.md" in str(path):
            return "CODE_WRITER_CONTENT"
        return None

    def fake_read_json(path: Path) -> dict | None:
        if path == orchestrator.TASK_CARD:
            return {"goal": "test", "in_scope": [], "out_of_scope": [], "acceptance_criteria": [], "constraints": []}
        if path == orchestrator.WORK_REPORT:
            return {"notes": "prev notes", "files_changed": ["file.py"]}
        if path == orchestrator.REVIEW_REPORT:
            return {
                "blocking_issues": [{"severity": "high", "file": "file.py", "reason": "fix it"}],
                "non_blocking_suggestions": ["sugg"],
            }
        if path == orchestrator.FIX_LIST:
            return {
                "task_id": "T-603",
                "round": 2,
                "fixes": [{"severity": "high", "file": "file.py", "reason": "fix it"}],
            }
        return None

    monkeypatch.setattr(orchestrator, "_read_text_optional", fake_read)
    monkeypatch.setattr(orchestrator, "_read_json_if_exists", fake_read_json)

    prompt = orchestrator._worker_prompt("T-603", 2)

    assert "AGENTS_CONTENT" not in prompt
    assert "FUNCTION INDEX" not in prompt
    assert "CODE_WRITER_CONTENT" in prompt
    assert "Current task_id: T-603, round: 2." in prompt
    assert "=== FIX LIST (round 2) ===" in prompt
    assert "[high] file.py: fix it" in prompt
    assert "work_report.json" in prompt
    assert "code-writer.md" in read_calls


def test_worker_prompt_round1_unchanged_includes_agents_and_function_index(monkeypatch) -> None:
    read_calls: list[str] = []

    def fake_read(path: Path) -> str | None:
        read_calls.append(path.name if path else str(path))
        if path.name == "AGENTS.md":
            return "AGENTS_CONTENT"
        if "code-writer.md" in str(path):
            return "CODE_WRITER_CONTENT"
        if path == orchestrator._worker_prompt_template_path():
            return None
        return None

    def fake_read_json(path: Path) -> dict | None:
        if path == orchestrator.TASK_CARD:
            return {"goal": "test", "in_scope": [], "out_of_scope": [], "acceptance_criteria": [], "constraints": []}
        return None

    monkeypatch.setattr(orchestrator, "_read_text_optional", fake_read)
    monkeypatch.setattr(orchestrator, "_read_json_if_exists", fake_read_json)

    prompt = orchestrator._worker_prompt("T-603", 1)

    assert "AGENTS_CONTENT" in prompt
    assert "FUNCTION INDEX" in prompt
    assert "CODE_WRITER_CONTENT" in prompt
    assert "=== FIX LIST" not in prompt


def test_worker_prompt_round3_slim_prompt(monkeypatch) -> None:
    def fake_read(path: Path) -> str | None:
        if "code-writer.md" in str(path):
            return "CODE_WRITER_CONTENT"
        return None

    def fake_read_json(path: Path) -> dict | None:
        if path == orchestrator.FIX_LIST:
            return {
                "task_id": "T-603",
                "round": 3,
                "fixes": [
                    {"severity": "medium", "file": "src/a.py", "reason": "refactor"},
                    {"severity": "low", "file": "src/b.py", "reason": "typo"},
                ],
            }
        if path == orchestrator.WORK_REPORT:
            return {"notes": "round 2 notes", "files_changed": ["src/a.py"]}
        if path == orchestrator.REVIEW_REPORT:
            return {
                "blocking_issues": [],
                "non_blocking_suggestions": ["sugg"],
            }
        return None

    monkeypatch.setattr(orchestrator, "_read_text_optional", fake_read)
    monkeypatch.setattr(orchestrator, "_read_json_if_exists", fake_read_json)

    prompt = orchestrator._worker_prompt("T-603", 3)

    assert "AGENTS_CONTENT" not in prompt
    assert "FUNCTION INDEX" not in prompt
    assert "=== FIX LIST (round 3) ===" in prompt
    assert "[medium] src/a.py: refactor" in prompt
    assert "[low] src/b.py: typo" in prompt


def test_worker_prompt_round2_no_fix_list(monkeypatch) -> None:
    def fake_read(path: Path) -> str | None:
        if "code-writer.md" in str(path):
            return "CODE_WRITER_CONTENT"
        return None

    def fake_read_json(path: Path) -> dict | None:
        if path == orchestrator.FIX_LIST:
            return None
        return None

    monkeypatch.setattr(orchestrator, "_read_text_optional", fake_read)
    monkeypatch.setattr(orchestrator, "_read_json_if_exists", fake_read_json)

    prompt = orchestrator._worker_prompt("T-603", 2)

    assert "AGENTS_CONTENT" not in prompt
    assert "=== FIX LIST (round 2) ===" in prompt
    assert "fixes:\n- <none>" in prompt


def test_worker_prompt_round1_uses_custom_template_file(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "AGENTS.md").write_text("AGENTS_CONTENT", encoding="utf-8")
    role_path = tmp_path / "docs" / "roles" / "code-writer.md"
    role_path.parent.mkdir(parents=True, exist_ok=True)
    role_path.write_text("CODE_WRITER_CONTENT", encoding="utf-8")
    orchestrator.TASK_CARD.write_text(
        json.dumps(
            {
                "task_id": "T-611",
                "goal": "Build via sections",
                "in_scope": [],
                "out_of_scope": [],
                "acceptance_criteria": [],
                "constraints": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    template_path = orchestrator._worker_prompt_template_path()
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(
        "CUSTOM {task_id} {round_num}\n{agents_md}\n{role_md}\n{task_card_section}{prior_context_section}",
        encoding="utf-8",
    )

    prompt = orchestrator._worker_prompt("T-611", 1)

    assert "CUSTOM" in prompt
    assert "AGENTS_CONTENT" in prompt
    assert "CODE_WRITER_CONTENT" in prompt
    assert "goal: Build via sections" in prompt


def test_worker_prompt_round1_succeeds_without_template_file(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "AGENTS.md").write_text("AGENTS_CONTENT", encoding="utf-8")
    role_path = tmp_path / "docs" / "roles" / "code-writer.md"
    role_path.parent.mkdir(parents=True, exist_ok=True)
    role_path.write_text("CODE_WRITER_CONTENT", encoding="utf-8")
    orchestrator.TASK_CARD.write_text(
        json.dumps(
            {
                "task_id": "T-611",
                "goal": "No template needed",
                "in_scope": [],
                "out_of_scope": [],
                "acceptance_criteria": [],
                "constraints": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    template_path = orchestrator._worker_prompt_template_path()
    template_path.unlink(missing_ok=True)

    prompt = orchestrator._worker_prompt("T-611", 1)

    assert "AGENTS_CONTENT" in prompt
    assert "CODE_WRITER_CONTENT" in prompt
    assert "No template needed" in prompt


def test_reviewer_prompt_includes_role_doc(monkeypatch) -> None:
    def fake_read(path: Path) -> str | None:
        if path.name == "reviewer.md":
            return "REVIEWER_CONTENT"
        if path == orchestrator._reviewer_prompt_template_path():
            return orchestrator.DEFAULT_REVIEWER_PROMPT_TEMPLATE
        return None

    monkeypatch.setattr(orchestrator, "_read_text_optional", fake_read)

    prompt = orchestrator._reviewer_prompt("T-603", 2)

    assert "REVIEWER_CONTENT" in prompt
    assert "Current task_id: T-603, round: 2." in prompt


def test_worker_prompt_uses_default_code_writer_doc_when_project_file_missing(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "docs" / "roles" / "code-writer.md").unlink()

    prompt = orchestrator._worker_prompt("T-613", 1)

    assert "code-writer.md (Default)" in prompt
    assert "You are the implementation worker." in prompt


def test_worker_prompt_uses_default_agents_doc_when_project_file_missing(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "AGENTS.md").unlink()

    prompt = orchestrator._worker_prompt("T-613", 1)

    assert "AGENTS.md (Default)" in prompt
    assert "Python target is 3.11+" in prompt


def test_worker_prompt_succeeds_when_agents_and_code_writer_docs_missing(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "docs" / "roles" / "code-writer.md").unlink()

    prompt = orchestrator._worker_prompt("T-613", 1)

    assert "AGENTS.md (Default)" in prompt
    assert "code-writer.md (Default)" in prompt


def test_persist_handoff_artifacts_are_structured(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    task_id = "T-701"
    orchestrator._persist_worker_handoff(
        task_id=task_id,
        round_num=1,
        work={
            "task_id": task_id,
            "round": 1,
            "head_sha": "abc123",
            "files_changed": ["src/loop_kit/orchestrator.py"],
            "tests": [{"name": "pytest", "result": "pass"}],
            "notes": "implemented changes",
        },
    )
    orchestrator._persist_reviewer_handoff(
        task_id=task_id,
        round_num=1,
        review={
            "task_id": task_id,
            "round": 1,
            "decision": "changes_required",
            "blocking_issues": [{"severity": "high", "file": "src/loop_kit/orchestrator.py", "reason": "fix metric"}],
            "non_blocking_suggestions": ["consider a smaller helper"],
        },
    )

    worker_handoff = json.loads((tmp_path / ".loop" / "handoff" / task_id / "worker_r1.json").read_text(encoding="utf-8"))
    reviewer_handoff = json.loads(
        (tmp_path / ".loop" / "handoff" / task_id / "reviewer_r1.json").read_text(encoding="utf-8")
    )

    for payload in (worker_handoff, reviewer_handoff):
        assert payload["task_id"] == task_id
        assert isinstance(payload.get("done"), list)
        assert isinstance(payload.get("open_questions"), list)
        assert isinstance(payload.get("next_actions"), list)
        assert isinstance(payload.get("evidence"), list)
        assert isinstance(payload.get("must_read_files"), list)

    assert "src/loop_kit/orchestrator.py" in reviewer_handoff["must_read_files"]


def test_reviewer_prompt_uses_default_role_doc_when_project_file_missing(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "docs" / "roles" / "reviewer.md").unlink()

    prompt = orchestrator._reviewer_prompt("T-613", 1)

    assert "reviewer.md (Default)" in prompt
    assert "reviewer agent in the PM loop" in prompt


def test_default_prompt_context_files_are_non_empty_and_practical() -> None:
    defaults_dir = Path(orchestrator.__file__).resolve().parent / "defaults"
    expectations = {
        "agents_md_default.txt": ["Python", "_", "pytest"],
        "code_writer_md_default.txt": ["task_card", "commit", "work_report.json"],
        "reviewer_md_default.txt": ["review_request", "acceptance", "review_report.json"],
    }

    for filename, keywords in expectations.items():
        text = (defaults_dir / filename).read_text(encoding="utf-8")
        assert len(text.strip()) >= 200
        for keyword in keywords:
            assert keyword in text


def test_read_text_with_default_prefers_project_file(tmp_path: Path) -> None:
    project_path = tmp_path / "AGENTS.md"
    project_path.write_text("PROJECT_AGENTS_OVERRIDE", encoding="utf-8")

    result = orchestrator._read_text_with_default(project_path, "agents_md_default.txt")

    assert result == "PROJECT_AGENTS_OVERRIDE"


def test_read_text_with_default_uses_packaged_default_when_project_file_missing(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "AGENTS.md"

    result = orchestrator._read_text_with_default(project_path, "agents_md_default.txt")

    assert "AGENTS.md (Default)" in result


def test_log_writes_jsonl_feed_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "LOGS_DIR", tmp_path)
    orchestrator._set_feed_task_id("T-700")
    orchestrator._set_feed_round(3)

    orchestrator._log("structured log message")

    feed_path = tmp_path / "feed.jsonl"
    assert feed_path.exists()
    entries = [json.loads(line) for line in feed_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert entries
    latest = entries[-1]
    assert set(latest.keys()) == {"ts", "level", "event", "data"}
    assert latest["level"] == "info"
    assert latest["event"] == orchestrator.FEED_LOG
    assert latest["data"]["message"] == "structured log message"
    assert latest["data"]["task_id"] == "T-700"
    assert latest["data"]["round"] == 3
    assert latest["data"]["role"] == "orchestrator"
    orchestrator._set_feed_task_id(None)


def test_feed_event_constants_defined() -> None:
    assert orchestrator.FEED_DISPATCH_START == "dispatch_start"
    assert orchestrator.FEED_DISPATCH_COMPLETE == "dispatch_complete"
    assert orchestrator.FEED_DISPATCH_FAIL == "dispatch_fail"
    assert orchestrator.FEED_DISPATCH_FIRST_ACTION == "dispatch_first_meaningful_action"
    assert orchestrator.FEED_DISPATCH_FIRST_STDOUT == "dispatch_first_stdout"
    assert orchestrator.FEED_DISPATCH_FIRST_WORK_ACTION == "dispatch_first_work_action"
    assert orchestrator.FEED_DISPATCH_ARTIFACT_WRITTEN == "dispatch_artifact_written"
    assert orchestrator.FEED_DISPATCH_PHASE_METRICS == "dispatch_phase_metrics"
    assert orchestrator.FEED_DISPATCH_RESUME == "dispatch_resume"
    assert orchestrator.FEED_ROUND_START == "round_start"
    assert orchestrator.FEED_ROUND_COMPLETE == "round_complete"
    assert orchestrator.FEED_REVIEW_VERDICT == "review_verdict"
    assert orchestrator.FEED_HEARTBEAT == "heartbeat"
    assert orchestrator.FEED_STATE_TRANSITION == "state_transition"


def test_run_auto_dispatch_emits_standardized_feed_events(monkeypatch) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(stdout_lines=['{"type":"thread.started","thread_id":"tid-1"}\n'], returncode=0),
    )

    def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
        _ = level
        events.append((event, dict(data or {})))

    monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)

    orchestrator._run_auto_dispatch(
        "worker",
        "codex",
        "ignored",
        30,
        task_id="T-626",
        round_num=1,
    )

    assert [event for event, _ in events if event == orchestrator.FEED_DISPATCH_START]
    assert [event for event, _ in events if event == orchestrator.FEED_DISPATCH_COMPLETE]
    for event, payload in events:
        if event in {
            orchestrator.FEED_DISPATCH_START,
            orchestrator.FEED_DISPATCH_COMPLETE,
        }:
            assert payload["task_id"] == "T-626"
            assert payload["round"] == 1
            assert payload["role"] == "worker"


def test_run_auto_dispatch_emits_first_stdout_once_after_dispatch_start(monkeypatch) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(
            stdout_lines=[
                '{"type":"thread.started","thread_id":"tid-1"}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"working..."}}\n',
            ],
            returncode=0,
        ),
    )

    def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
        _ = level
        events.append((event, dict(data or {})))

    monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)

    orchestrator._run_auto_dispatch(
        "worker",
        "codex",
        "ignored",
        30,
        task_id="T-626",
        round_num=1,
    )

    start_indexes = [idx for idx, (event, _payload) in enumerate(events) if event == orchestrator.FEED_DISPATCH_START]
    stdout_events = [
        (idx, payload)
        for idx, (event, payload) in enumerate(events)
        if event == orchestrator.FEED_DISPATCH_FIRST_STDOUT and payload.get("status") != "not_observed"
    ]
    assert start_indexes
    assert len(stdout_events) == 1
    assert start_indexes[0] < stdout_events[0][0]
    assert isinstance(stdout_events[0][1]["latency_ms"], int)


def test_run_auto_dispatch_first_work_action_ignores_summary_only_lines(monkeypatch) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(
            stdout_lines=[
                '{"type":"item.completed","item":{"type":"agent_message","text":"analyzing task"}}\n',
                '{"type":"item.started","item":{"type":"command_execution","command":"git status --short"}}\n',
                '{"type":"item.started","item":{"type":"command_execution","command":"git diff --stat"}}\n',
            ],
            returncode=0,
        ),
    )

    def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
        events.append((event, level, dict(data or {})))

    monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)

    orchestrator._run_auto_dispatch(
        "worker",
        "codex",
        "ignored",
        30,
        task_id="T-626",
        round_num=1,
    )

    work_events = [
        payload
        for event, _level, payload in events
        if event == orchestrator.FEED_DISPATCH_FIRST_WORK_ACTION and payload.get("status") != "not_observed"
    ]
    assert len(work_events) == 1
    assert work_events[0]["signal"] == "item.started"
    assert work_events[0]["item_type"] == "command_execution"
    assert isinstance(work_events[0]["latency_ms"], int)


def test_classify_dispatch_action_categorizes_codex_event_payloads() -> None:
    search_line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "rg --line-number dispatch_phase_metrics src/loop_kit/orchestrator.py"},
        },
        ensure_ascii=False,
    )
    read_line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "Get-Content README.md"},
        },
        ensure_ascii=False,
    )
    edit_line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "file_change", "changes": [{"path": "src/loop_kit/orchestrator.py"}]},
        },
        ensure_ascii=False,
    )
    test_line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "uv run --group dev pytest tests/test_orchestrator.py"},
        },
        ensure_ascii=False,
    )
    unknown_line = json.dumps(
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "git status --short"},
        },
        ensure_ascii=False,
    )

    assert orchestrator._classify_dispatch_action(orchestrator.BACKEND_CODEX, search_line) == {
        "category": "search",
        "signal": "item.started",
        "item_type": "command_execution",
    }
    assert orchestrator._classify_dispatch_action(orchestrator.BACKEND_CODEX, read_line) == {
        "category": "read",
        "signal": "item.started",
        "item_type": "command_execution",
    }
    assert orchestrator._classify_dispatch_action(orchestrator.BACKEND_CODEX, edit_line) == {
        "category": "edit",
        "signal": "item.started",
        "item_type": "file_change",
    }
    assert orchestrator._classify_dispatch_action(orchestrator.BACKEND_CODEX, test_line) == {
        "category": "test",
        "signal": "item.started",
        "item_type": "command_execution",
    }
    assert orchestrator._classify_dispatch_action(orchestrator.BACKEND_CODEX, unknown_line) == {
        "category": "unknown",
        "signal": "item.started",
        "item_type": "command_execution",
    }


def test_run_auto_dispatch_emits_first_meaningful_action_metric(monkeypatch) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt: (["codex.exe", "exec", "short instruction"], None, "STDIN_PAYLOAD"),
    )
    monkeypatch.setattr(
        orchestrator.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProc(
            stdout_lines=[
                '{"type":"item.completed","item":{"type":"command_execution","command":"uv run --group dev pytest"}}\n'
            ],
            returncode=0,
        ),
    )

    def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
        events.append((event, level, dict(data or {})))

    monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)

    orchestrator._run_auto_dispatch(
        "worker",
        "codex",
        "ignored",
        30,
        task_id="T-626",
        round_num=1,
    )

    metric_events = [payload for event, _level, payload in events if event == orchestrator.FEED_DISPATCH_FIRST_ACTION]
    assert metric_events
    observed = [payload for payload in metric_events if payload.get("status") != "not_observed"]
    assert observed
    assert observed[0]["task_id"] == "T-626"
    assert observed[0]["round"] == 1
    assert observed[0]["role"] == "worker"
    assert isinstance(observed[0]["latency_ms"], int)
    assert observed[0]["signal_type"] == "summary_signal"
    assert str(observed[0].get("summary", "")).startswith("[worker] Running:")


def test_run_auto_dispatch_emits_resume_fallback_event(monkeypatch) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_write_dispatch_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_agent_command",
        lambda backend, prompt, resume_session_id=None: (
            ["codex.exe", "exec", "short instruction"],
            resume_session_id,
            "STDIN_PAYLOAD",
        ),
    )

    calls = {"count": 0}

    def fake_popen(cmd, **kwargs):
        _ = (cmd, kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            return _FakeProc(stdout_lines=[], stderr_lines=["no rollout found for thread id stale-session\n"], returncode=1)
        return _FakeProc(
            stdout_lines=['{"type":"item.completed","item":{"type":"agent_message","text":"resumed"}}\n'],
            returncode=0,
        )

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_popen)

    def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
        events.append((event, level, dict(data or {})))

    monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)

    orchestrator._run_auto_dispatch(
        "worker",
        "codex",
        "ignored",
        30,
        dispatch_retries=0,
        task_id="T-626",
        round_num=1,
        resume_session_id="stale-session",
    )

    fallback_events = [
        payload
        for event, _level, payload in events
        if event == orchestrator.FEED_DISPATCH_RESUME and payload.get("status") == "fallback_invalid_resume"
    ]
    assert fallback_events
    assert fallback_events[0]["session_id"] == "stale-session"
    assert calls["count"] == 2


def test_save_state_emits_state_transition_feed_event(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    captured: list[tuple[str, dict[str, object]]] = []

    def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None) -> None:
        _ = level
        captured.append((event, dict(data or {})))

    monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)

    orchestrator._save_state(
        {
            "state": orchestrator.STATE_AWAITING_WORK,
            "round": 1,
            "task_id": "T-800",
            "base_sha": "base-sha",
        }
    )

    assert captured
    event, payload = captured[-1]
    assert event == orchestrator.FEED_STATE_TRANSITION
    assert payload["task_id"] == "T-800"
    assert payload["round"] == 1
    assert payload["role"] == "orchestrator"
    assert payload["from_state"] is None
    assert payload["to_state"] == orchestrator.STATE_AWAITING_WORK


def test_configure_loop_paths_resets_log_dir_ensure_flag(tmp_path: Path, monkeypatch) -> None:
    original_root = orchestrator.ROOT
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "_LOGS_DIR_ENSURED", False)
    monkeypatch.setattr(orchestrator, "_LOGS_DIR_ENSURED_PATH", None)
    orchestrator._set_feed_task_id(None)

    orchestrator._configure_loop_paths(".loop-a")
    orchestrator._log("first log")
    assert (tmp_path / ".loop-a" / "logs" / "orchestrator.log").exists()

    orchestrator._configure_loop_paths(".loop-b")
    assert orchestrator._LOGS_DIR_ENSURED is False
    orchestrator._log("second log")

    assert (tmp_path / ".loop-b" / "logs" / "orchestrator.log").exists()
    assert (tmp_path / ".loop-b" / "logs" / "feed.jsonl").exists()

    monkeypatch.setattr(orchestrator, "ROOT", original_root)
    orchestrator._configure_loop_paths(".loop")


def test_feed_event_filters_by_task_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "LOGS_DIR", tmp_path)
    orchestrator._set_feed_task_id("T-777")

    orchestrator._feed_event("same_task", data={"task_id": "T-777", "x": 1})
    orchestrator._feed_event("other_task", data={"task_id": "T-888", "x": 2})
    orchestrator._feed_event("missing_task", data={"x": 3})

    entries = [
        json.loads(line) for line in (tmp_path / "feed.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(entries) == 2
    assert entries[0]["event"] == "same_task"
    assert entries[0]["data"]["task_id"] == "T-777"
    assert entries[1]["event"] == "missing_task"
    assert entries[1]["data"]["task_id"] == "T-777"

    orchestrator._set_feed_task_id(None)


def test_main_run_parses_artifact_timeout(monkeypatch) -> None:
    captured: dict[str, int | bool | None] = {}

    def fake_cmd_run(
        config: orchestrator.RunConfig,
        single_round: bool,
        round_num: int | None,
        resume: bool = False,
        reset: bool = False,
    ) -> None:
        _ = reset
        captured["artifact_timeout"] = config.artifact_timeout
        captured["single_round"] = single_round
        captured["round_num"] = round_num
        captured["resume"] = resume
        captured["verbose"] = config.verbose

    monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orchestrator.py",
            "run",
            "--artifact-timeout",
            "123",
            "--verbose",
            "--single-round",
            "--round",
            "7",
        ],
    )

    orchestrator.main()

    assert captured["artifact_timeout"] == 123
    assert captured["single_round"] is True
    assert captured["round_num"] == 7
    assert captured["resume"] is False
    assert captured["verbose"] is True


def test_main_run_dispatch_timeout_defaults_to_unlimited(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def fake_cmd_run(
        config: orchestrator.RunConfig,
        single_round: bool,
        round_num: int | None,
        resume: bool = False,
        reset: bool = False,
    ) -> None:
        _ = (single_round, round_num, resume, reset)
        captured["dispatch_timeout"] = config.dispatch_timeout

    monkeypatch.setattr(orchestrator, "_load_config", lambda: {})
    monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "run"])

    orchestrator.main()

    assert orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC == 0
    assert captured["dispatch_timeout"] == 0


def test_main_run_parses_dispatch_timeout(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def fake_cmd_run(
        config: orchestrator.RunConfig,
        single_round: bool,
        round_num: int | None,
        resume: bool = False,
        reset: bool = False,
    ) -> None:
        _ = (single_round, round_num, resume, reset)
        captured["dispatch_timeout"] = config.dispatch_timeout

    monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orchestrator.py",
            "run",
            "--dispatch-timeout",
            "47",
        ],
    )

    orchestrator.main()

    assert captured["dispatch_timeout"] == 47


def test_main_run_parses_dispatch_retry_flags(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def fake_cmd_run(
        config: orchestrator.RunConfig,
        single_round: bool,
        round_num: int | None,
        resume: bool = False,
        reset: bool = False,
    ) -> None:
        _ = (single_round, round_num, resume, reset)
        captured["dispatch_retries"] = config.dispatch_retries
        captured["dispatch_retry_base_sec"] = config.dispatch_retry_base_sec

    monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orchestrator.py",
            "run",
            "--dispatch-retries",
            "4",
            "--dispatch-retry-base-sec",
            "7",
        ],
    )

    orchestrator.main()

    assert captured["dispatch_retries"] == 4
    assert captured["dispatch_retry_base_sec"] == 7


def test_main_run_parses_max_session_rounds(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def fake_cmd_run(
        config: orchestrator.RunConfig,
        single_round: bool,
        round_num: int | None,
        resume: bool = False,
        reset: bool = False,
    ) -> None:
        _ = (single_round, round_num, resume, reset)
        captured["max_session_rounds"] = config.max_session_rounds

    monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "orchestrator.py",
            "run",
            "--max-session-rounds",
            "2",
        ],
    )

    orchestrator.main()

    assert captured["max_session_rounds"] == 2


def _configure_loop_paths(monkeypatch, tmp_path: Path) -> None:
    loop_dir = tmp_path / ".loop"
    logs_dir = loop_dir / "logs"
    runtime_dir = loop_dir / "runtime"
    loop_dir.mkdir()
    logs_dir.mkdir()
    runtime_dir.mkdir()
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "LOOP_DIR", loop_dir)
    monkeypatch.setattr(orchestrator, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(orchestrator, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(orchestrator, "ARCHIVE_DIR", loop_dir / "archive")
    monkeypatch.setattr(orchestrator, "STATE_FILE", loop_dir / "state.json")
    monkeypatch.setattr(orchestrator, "_STATE_BACKUP", loop_dir / ".state.json.bak")
    monkeypatch.setattr(orchestrator, "TASK_CARD", loop_dir / "task_card.json")
    monkeypatch.setattr(orchestrator, "FIX_LIST", loop_dir / "fix_list.json")
    monkeypatch.setattr(orchestrator, "WORK_REPORT", loop_dir / "work_report.json")
    monkeypatch.setattr(orchestrator, "REVIEW_REQ", loop_dir / "review_request.json")
    monkeypatch.setattr(orchestrator, "REVIEW_REPORT", loop_dir / "review_report.json")
    monkeypatch.setattr(orchestrator, "LOCK_FILE", loop_dir / "lock")
    monkeypatch.setattr(orchestrator, "_CONFIG_FILE", loop_dir / "config.json")
    monkeypatch.setattr(orchestrator, "TASK_PACKET", loop_dir / "task_packet.json")
    monkeypatch.setattr(orchestrator, "_HANDOFF_DIR", loop_dir / "handoff")
    monkeypatch.setattr(orchestrator, "_CONTEXT_DIR", loop_dir / "context")
    monkeypatch.setattr(orchestrator, "_MODULE_MAP_FILE", loop_dir / "context" / "module_map.json")
    monkeypatch.setattr(orchestrator, "_PROJECT_FACTS_FILE", loop_dir / "context" / "project_facts.md")
    monkeypatch.setattr(orchestrator, "_PITFALLS_FILE", loop_dir / "context" / "pitfalls.md")
    monkeypatch.setattr(orchestrator, "_PATTERNS_FILE", loop_dir / "context" / "patterns.jsonl")
    orchestrator._set_feed_task_id(None)
    (tmp_path / "AGENTS.md").write_text("AGENTS_CONTENT", encoding="utf-8")
    role_dir = tmp_path / "docs" / "roles"
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "code-writer.md").write_text("CODE_WRITER_CONTENT", encoding="utf-8")
    (role_dir / "reviewer.md").write_text("REVIEWER_CONTENT", encoding="utf-8")
    templates_dir = loop_dir / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / "worker_prompt.txt").write_text(
        orchestrator.DEFAULT_WORKER_PROMPT_TEMPLATE,
        encoding="utf-8",
    )
    (templates_dir / "reviewer_prompt.txt").write_text(
        orchestrator.DEFAULT_REVIEWER_PROMPT_TEMPLATE,
        encoding="utf-8",
    )


def _run_config(task_path: str, **overrides: object) -> orchestrator.RunConfig:
    return orchestrator.RunConfig(task_path=task_path, **overrides)


def _configure_default_knowledge_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    defaults_dir = tmp_path / "defaults"
    defaults_dir.mkdir(parents=True, exist_ok=True)
    facts_path = defaults_dir / "facts.jsonl"
    pitfalls_path = defaults_dir / "pitfalls.jsonl"
    patterns_path = defaults_dir / "patterns.jsonl"
    monkeypatch.setattr(orchestrator, "_DEFAULTS_DIR", defaults_dir)
    monkeypatch.setattr(orchestrator, "_DEFAULT_FACTS_JSONL", facts_path)
    monkeypatch.setattr(orchestrator, "_DEFAULT_PITFALLS_JSONL", pitfalls_path)
    monkeypatch.setattr(orchestrator, "_DEFAULT_PATTERNS_JSONL", patterns_path)
    monkeypatch.setattr(orchestrator, "_configure_loop_paths", lambda loop_dir=".loop": orchestrator._snapshot_global_paths())
    return facts_path, pitfalls_path, patterns_path


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


class TestKnowledgeCli:
    def test_knowledge_list_prints_table_with_category_filter(self, tmp_path: Path, monkeypatch, capsys) -> None:
        facts_path, pitfalls_path, patterns_path = _configure_default_knowledge_paths(monkeypatch, tmp_path)
        _write_jsonl(facts_path, [{"fact": "single-file rule", "category": "facts"}])
        _write_jsonl(pitfalls_path, [{"pitfall": "stale lock", "category": "pitfalls"}])
        _write_jsonl(
            patterns_path,
            [
                {
                    "pattern": "run tests",
                    "category": "workflow",
                    "confidence": 0.9,
                    "source": "manual",
                    "source_version": "2026-04-01T00:00:00Z",
                }
            ],
        )

        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "knowledge", "list", "--category", "workflow"],
        )

        orchestrator.main()
        out = capsys.readouterr().out
        assert "type" in out and "category" in out and "text" in out
        assert "run tests" in out
        assert "single-file rule" not in out
        assert "stale lock" not in out

    def test_knowledge_add_appends_pattern_entry(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _, _, patterns_path = _configure_default_knowledge_paths(monkeypatch, tmp_path)
        _write_jsonl(
            patterns_path,
            [
                {
                    "pattern": "existing pattern",
                    "category": "quality",
                    "confidence": 0.5,
                }
            ],
        )

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "orchestrator.py",
                "knowledge",
                "add",
                "--pattern",
                "new pattern",
                "--category",
                "workflow",
                "--confidence",
                "0.8",
                "--source",
                "review",
            ],
        )

        orchestrator.main()
        out = capsys.readouterr().out
        assert "Added pattern" in out
        entries = [
            json.loads(line)
            for line in patterns_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(entries) == 2
        new_entry = entries[-1]
        assert new_entry["pattern"] == "new pattern"
        assert new_entry["category"] == "workflow"
        assert new_entry["confidence"] == 0.8
        assert new_entry["source"] == "review"
        assert isinstance(new_entry["source_version"], str) and new_entry["source_version"]

    def test_knowledge_prune_removes_entries_with_old_source_version(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        facts_path, pitfalls_path, patterns_path = _configure_default_knowledge_paths(monkeypatch, tmp_path)
        now = datetime.now(UTC)
        old_iso = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_iso = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_jsonl(
            facts_path,
            [
                {"fact": "old fact", "source_version": old_iso},
                {"fact": "fresh fact", "source_version": fresh_iso},
            ],
        )
        _write_jsonl(
            pitfalls_path,
            [
                {"pitfall": "old pitfall", "source_version": old_iso},
                {"pitfall": "fresh pitfall", "source_version": fresh_iso},
            ],
        )
        _write_jsonl(
            patterns_path,
            [
                {"pattern": "old pattern", "category": "workflow", "confidence": 0.2, "source_version": old_iso},
                {"pattern": "keep missing source_version", "category": "workflow", "confidence": 0.3},
            ],
        )

        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "knowledge", "prune", "--older-than", "30"],
        )

        orchestrator.main()
        out = capsys.readouterr().out
        assert "removed_total=3" in out

        facts_entries = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        pitfalls_entries = [
            json.loads(line) for line in pitfalls_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        patterns_entries = [
            json.loads(line) for line in patterns_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert [entry["fact"] for entry in facts_entries] == ["fresh fact"]
        assert [entry["pitfall"] for entry in pitfalls_entries] == ["fresh pitfall"]
        assert {entry["pattern"] for entry in patterns_entries} == {"keep missing source_version"}

    def test_knowledge_dedupe_removes_duplicates_and_keeps_higher_confidence(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        facts_path, pitfalls_path, patterns_path = _configure_default_knowledge_paths(monkeypatch, tmp_path)
        _write_jsonl(
            facts_path,
            [
                {"fact": "single-file rule", "category": "facts"},
                {"fact": "single-file rule", "category": "facts"},
            ],
        )
        _write_jsonl(
            pitfalls_path,
            [
                {"pitfall": "stale lock", "category": "pitfalls"},
                {"pitfall": "stale lock", "category": "pitfalls"},
            ],
        )
        _write_jsonl(
            patterns_path,
            [
                {"pattern": "run tests", "category": "workflow", "confidence": 0.2},
                {"pattern": "run tests", "category": "workflow", "confidence": 0.9},
                {"pattern": "run lint", "category": "workflow", "confidence": 0.5},
            ],
        )

        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "knowledge", "dedupe"],
        )

        orchestrator.main()
        out = capsys.readouterr().out
        assert "facts_removed=1" in out
        assert "pitfalls_removed=1" in out
        assert "patterns_removed=1" in out

        facts_entries = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        pitfalls_entries = [
            json.loads(line) for line in pitfalls_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        patterns_entries = [
            json.loads(line) for line in patterns_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert len(facts_entries) == 1
        assert len(pitfalls_entries) == 1
        assert len(patterns_entries) == 2
        by_pattern = {(entry["category"], entry["pattern"]): entry for entry in patterns_entries}
        assert by_pattern[("workflow", "run tests")]["confidence"] == 0.9


def test_cmd_index_generates_module_map_with_expected_shape(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    src = tmp_path / "src" / "loop_kit"
    nested = src / "pkg"
    nested.mkdir(parents=True, exist_ok=True)
    alpha_text = '"""Alpha module docs.\nMore details."""\n\ndef hello():\n    pass\n\nclass Greeter:\n    pass\n'
    alpha_path = src / "alpha.py"
    alpha_path.write_text(alpha_text, encoding="utf-8")
    beta_text = "async def run():\n    return None\n"
    beta_path = nested / "beta.py"
    beta_path.write_text(beta_text, encoding="utf-8")

    orchestrator.cmd_index()

    module_map = json.loads(orchestrator._MODULE_MAP_FILE.read_text(encoding="utf-8"))
    assert isinstance(module_map.get("generated_at"), str) and module_map["generated_at"]
    assert module_map["total_files"] == 2

    files = module_map["files"]
    assert isinstance(files, list)
    by_path = {entry["path"]: entry for entry in files}
    assert sorted(by_path) == ["src/loop_kit/alpha.py", "src/loop_kit/pkg/beta.py"]

    alpha = by_path["src/loop_kit/alpha.py"]
    assert alpha["docstring"] == "Alpha module docs."
    assert any(item.startswith("def hello:L") for item in alpha["exports"])
    assert any(item.startswith("class Greeter:L") for item in alpha["exports"])
    assert alpha["loc"] == len(alpha_text.splitlines())
    assert alpha["size_bytes"] == alpha_path.stat().st_size
    assert alpha["last_modified"] == alpha_path.stat().st_mtime_ns

    beta = by_path["src/loop_kit/pkg/beta.py"]
    assert beta["docstring"] == ""
    assert beta["exports"] == ["async def run:L1"]
    assert beta["loc"] == len(beta_text.splitlines())
    assert beta["size_bytes"] == beta_path.stat().st_size
    assert beta["last_modified"] == beta_path.stat().st_mtime_ns


def test_cmd_index_incremental_reuses_unchanged_entries(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    src = tmp_path / "src" / "loop_kit"
    src.mkdir(parents=True, exist_ok=True)
    alpha_path = src / "alpha.py"
    beta_path = src / "beta.py"
    alpha_path.write_text("def alpha():\n    pass\n", encoding="utf-8")
    beta_path.write_text("def beta():\n    pass\n", encoding="utf-8")

    orchestrator.cmd_index()
    first_map = json.loads(orchestrator._MODULE_MAP_FILE.read_text(encoding="utf-8"))
    first_by_path = {entry["path"]: entry for entry in first_map["files"]}

    updated_beta = "def beta():\n    return 1\n"
    beta_path.write_text(updated_beta, encoding="utf-8")
    old_beta_mtime = first_by_path["src/loop_kit/beta.py"]["last_modified"]
    os.utime(beta_path, ns=(old_beta_mtime + 1_000_000_000, old_beta_mtime + 1_000_000_000))

    calls: list[str] = []
    real_index = orchestrator._index_module_file

    def spy_index(path: Path, rel_path: str, stat_result) -> dict:
        _ = path
        _ = stat_result
        calls.append(rel_path)
        return real_index(path, rel_path, stat_result)

    monkeypatch.setattr(orchestrator, "_index_module_file", spy_index)
    orchestrator.cmd_index()

    second_map = json.loads(orchestrator._MODULE_MAP_FILE.read_text(encoding="utf-8"))
    second_by_path = {entry["path"]: entry for entry in second_map["files"]}

    assert calls == ["src/loop_kit/beta.py"]
    assert second_by_path["src/loop_kit/alpha.py"] == first_by_path["src/loop_kit/alpha.py"]
    assert second_by_path["src/loop_kit/beta.py"]["loc"] == len(updated_beta.splitlines())
    assert (
        second_by_path["src/loop_kit/beta.py"]["last_modified"]
        != first_by_path["src/loop_kit/beta.py"]["last_modified"]
    )


def test_archive_bus_file_copies_to_round_path(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    orchestrator.WORK_REPORT.write_text('{"task_id":"T-604","round":1}\n', encoding="utf-8")

    archived = orchestrator._archive_bus_file(
        orchestrator.WORK_REPORT,
        "T-604",
        1,
        "work_report",
    )

    expected = orchestrator.LOOP_DIR / "archive" / "T-604" / "r1_work_report.json"
    assert archived == expected
    assert expected.read_text(encoding="utf-8") == '{"task_id":"T-604","round":1}\n'


def test_archive_bus_file_missing_source_is_noop(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    archived = orchestrator._archive_bus_file(
        orchestrator.WORK_REPORT,
        "T-604",
        1,
        "work_report",
    )

    assert archived is None
    assert (orchestrator.LOOP_DIR / "archive" / "T-604").exists() is False


def test_archive_subcommand_lists_files(tmp_path: Path, monkeypatch, capsys) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    archive_dir = orchestrator.LOOP_DIR / "archive" / "T-604"
    archive_dir.mkdir(parents=True)
    (archive_dir / "r1_work_report.json").write_text("{}\n", encoding="utf-8")
    (archive_dir / "r1_review_report.json").write_text("{}\n", encoding="utf-8")

    orchestrator.cmd_archive("T-604")

    out = capsys.readouterr().out
    assert "Archive directory:" in out
    assert "r1_work_report.json" in out
    assert "r1_review_report.json" in out


def test_archive_subcommand_restore_round_file_to_loop(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    archive_dir = orchestrator.LOOP_DIR / "archive" / "T-604"
    archive_dir.mkdir(parents=True)
    archived_payload = '{"task_id":"T-604","round":1}\n'
    (archive_dir / "r1_work_report.json").write_text(archived_payload, encoding="utf-8")

    orchestrator.cmd_archive("T-604", restore="r1_work_report")

    assert orchestrator.WORK_REPORT.read_text(encoding="utf-8") == archived_payload


def test_round2_start_archives_round1_bus_files(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "archive old round bus files"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_AWAITING_WORK,
                "round": 2,
                "task_id": "T-604",
                "base_sha": "base-sha",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    orchestrator.WORK_REPORT.write_text(
        json.dumps({"task_id": "T-604", "round": 1, "head_sha": "old-head"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.REVIEW_REPORT.write_text(
        json.dumps({"task_id": "T-604", "round": 1, "decision": "changes_required"}, ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_wait(path: Path, description: str, **kwargs) -> dict | None:
        _ = (description, kwargs)
        if path == orchestrator.WORK_REPORT:
            return {
                "task_id": "T-604",
                "round": 2,
                "head_sha": "new-head",
                "files_changed": ["tools/orchestrator.py"],
                "tests": [],
                "notes": "round2 work",
            }
        if path == orchestrator.REVIEW_REPORT:
            return {
                "task_id": "T-604",
                "round": 2,
                "decision": "approve",
                "blocking_issues": [],
                "non_blocking_suggestions": [],
            }
        return None

    monkeypatch.setattr(orchestrator, "_wait_for_file", fake_wait)
    monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}->{head}")
    monkeypatch.setattr(orchestrator, "_log_oneline", lambda base, head: f"log {base}->{head}")

    orchestrator.cmd_run(
        _run_config(str(task_path), allow_dirty=True),
        single_round=True,
        round_num=2,
    )

    archive_dir = orchestrator.LOOP_DIR / "archive" / "T-604"
    archived_work = json.loads((archive_dir / "r2_work_report.json").read_text(encoding="utf-8"))
    archived_review = json.loads((archive_dir / "r2_review_report.json").read_text(encoding="utf-8"))
    assert archived_work["round"] == 1
    assert archived_work["head_sha"] == "old-head"
    assert archived_review["round"] == 1
    assert archived_review["decision"] == "changes_required"


def test_single_round_archives_state_before_overwrite(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "single-round state archive"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_AWAITING_WORK,
                "round": 1,
                "task_id": "T-604",
                "base_sha": "base-sha",
                "snapshot": "before-single-round-overwrite",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_wait(path: Path, description: str, **kwargs) -> dict | None:
        _ = (description, kwargs)
        if path == orchestrator.WORK_REPORT:
            return {
                "task_id": "T-604",
                "round": 1,
                "head_sha": "new-head",
                "files_changed": ["tools/orchestrator.py"],
                "tests": [],
                "notes": "round1 work",
            }
        if path == orchestrator.REVIEW_REPORT:
            return {
                "task_id": "T-604",
                "round": 1,
                "decision": "approve",
                "blocking_issues": [],
                "non_blocking_suggestions": [],
            }
        return None

    monkeypatch.setattr(orchestrator, "_wait_for_file", fake_wait)
    monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}->{head}")
    monkeypatch.setattr(orchestrator, "_log_oneline", lambda base, head: f"log {base}->{head}")

    orchestrator.cmd_run(
        _run_config(str(task_path), allow_dirty=True),
        single_round=True,
        round_num=1,
    )

    archived_state = json.loads(
        (orchestrator.LOOP_DIR / "archive" / "T-604" / "r1_state.json").read_text(encoding="utf-8")
    )
    assert archived_state["snapshot"] == "before-single-round-overwrite"


def test_cmd_run_exits_4_on_dirty_worktree(monkeypatch, capsys) -> None:
    monkeypatch.setattr(orchestrator, "_dirty_tracked_paths", lambda: ["tools/orchestrator.py"])

    class _NoopLock:
        def release(self) -> None:
            return None

    monkeypatch.setattr(orchestrator, "_acquire_run_lock", lambda: _NoopLock())

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_run(
            _run_config(".loop/task_card.json"),
            single_round=False,
            round_num=None,
        )

    assert exc.value.code == 4
    err = capsys.readouterr().err
    assert "dirty git working tree" in err
    assert "tools/orchestrator.py" in err


def test_cmd_run_allow_dirty_bypasses_guard(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_dirty_tracked_paths", lambda: ["tools/orchestrator.py"])
    called: dict[str, bool] = {"single_round_called": False}

    def fake_single_round(**kwargs) -> None:
        _ = kwargs
        called["single_round_called"] = True

    monkeypatch.setattr(orchestrator, "_run_single_round", fake_single_round)

    orchestrator.cmd_run(
        _run_config(".loop/task_card.json", allow_dirty=True),
        single_round=True,
        round_num=1,
    )

    assert called["single_round_called"] is True


def test_single_round_processes_exactly_one_round_then_exits(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps(
            {
                "task_id": "T-604",
                "goal": "single round test",
                "acceptance_criteria": [],
                "constraints": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_AWAITING_WORK,
                "round": 1,
                "task_id": "T-604",
                "base_sha": "base-sha",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    work_report = {
        "task_id": "T-604",
        "round": 1,
        "head_sha": "head-sha",
        "files_changed": ["tools/orchestrator.py"],
        "tests": [],
        "notes": "ok",
    }
    review_report = {
        "task_id": "T-604",
        "round": 1,
        "decision": "changes_required",
        "blocking_issues": [{"id": "R1", "severity": "high", "file": "tools/orchestrator.py"}],
        "non_blocking_suggestions": ["n1"],
    }

    wait_calls: list[Path] = []

    def fake_wait(path: Path, description: str, **kwargs) -> dict | None:
        _ = (description, kwargs)
        wait_calls.append(path)
        if path == orchestrator.WORK_REPORT:
            return work_report
        if path == orchestrator.REVIEW_REPORT:
            return review_report
        return None

    monkeypatch.setattr(orchestrator, "_wait_for_file", fake_wait)
    monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}->{head}")
    monkeypatch.setattr(orchestrator, "_log_oneline", lambda base, head: f"log {base}->{head}")

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=True,
        round_num=1,
    )

    assert wait_calls == [orchestrator.WORK_REPORT, orchestrator.REVIEW_REPORT]
    fix_list = json.loads(orchestrator.FIX_LIST.read_text(encoding="utf-8"))
    state = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
    assert fix_list["task_id"] == "T-604"
    assert fix_list["round"] == 2
    assert fix_list["prior_round_notes"] == "ok"
    assert fix_list["prior_review_non_blocking"] == ["n1"]
    assert state["state"] == orchestrator.STATE_AWAITING_WORK
    assert state["round"] == 2


def test_single_round_skips_non_dict_blocking_issues(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "mixed blocking issues"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_AWAITING_WORK,
                "round": 1,
                "task_id": "T-604",
                "base_sha": "base-sha",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    work_report = {
        "task_id": "T-604",
        "round": 1,
        "head_sha": "head-sha",
        "files_changed": ["tools/orchestrator.py"],
        "tests": [],
        "notes": "ok",
    }
    review_report = {
        "task_id": "T-604",
        "round": 1,
        "decision": "changes_required",
        "blocking_issues": [
            {"id": "R1", "severity": "high", "file": "tools/orchestrator.py", "reason": "fix me"},
            "bad-item",
            None,
            123,
        ],
        "non_blocking_suggestions": [],
    }

    def fake_wait(path: Path, description: str, **kwargs) -> dict | None:
        _ = (description, kwargs)
        if path == orchestrator.WORK_REPORT:
            return work_report
        if path == orchestrator.REVIEW_REPORT:
            return review_report
        return None

    monkeypatch.setattr(orchestrator, "_wait_for_file", fake_wait)
    monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}->{head}")
    monkeypatch.setattr(orchestrator, "_log_oneline", lambda base, head: f"log {base}->{head}")

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=True,
        round_num=1,
    )

    fix_list = json.loads(orchestrator.FIX_LIST.read_text(encoding="utf-8"))
    state = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))

    assert fix_list["fixes"] == [review_report["blocking_issues"][0]]
    assert state["state"] == orchestrator.STATE_AWAITING_WORK
    assert state["round"] == 2


def test_single_round_invalid_work_report_sets_invalid_outcome(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "invalid report test"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_AWAITING_WORK,
                "round": 1,
                "task_id": "T-604",
                "base_sha": "base-sha",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_wait(path: Path, description: str, **kwargs) -> dict | None:
        _ = (description, kwargs)
        if path == orchestrator.WORK_REPORT:
            return {
                "task_id": "T-604",
                "head_sha": "head-sha",
            }
        return None

    monkeypatch.setattr(orchestrator, "_wait_for_file", fake_wait)

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_run(
            _run_config(str(task_path), allow_dirty=True),
            single_round=True,
            round_num=1,
        )

    assert exc.value.code == 3
    state = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
    assert state["outcome"] == "invalid_work_report"
    assert "round" in state["error"]


def test_single_round_invalid_review_report_sets_invalid_outcome(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "invalid review report test"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_AWAITING_WORK,
                "round": 1,
                "task_id": "T-604",
                "base_sha": "base-sha",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    work_report = {
        "task_id": "T-604",
        "round": 1,
        "head_sha": "head-sha",
        "files_changed": ["tools/orchestrator.py"],
        "tests": [],
        "notes": "ok",
    }

    def fake_wait(path: Path, description: str, **kwargs) -> dict | None:
        _ = (description, kwargs)
        if path == orchestrator.WORK_REPORT:
            return work_report
        if path == orchestrator.REVIEW_REPORT:
            return {
                "task_id": "T-604",
                "round": 1,
                "decision": "maybe",
            }
        return None

    monkeypatch.setattr(orchestrator, "_wait_for_file", fake_wait)
    monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}->{head}")
    monkeypatch.setattr(orchestrator, "_log_oneline", lambda base, head: f"log {base}->{head}")

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_run(
            _run_config(str(task_path), allow_dirty=True),
            single_round=True,
            round_num=1,
        )

    assert exc.value.code == 3
    state = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
    assert state["outcome"] == "invalid_review_report"
    assert "decision" in state["error"]


def test_load_task_card_rejects_invalid_json(tmp_path: Path, capsys) -> None:
    task_path = tmp_path / "task_input.json"
    task_path.write_text("{oops\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        orchestrator._load_task_card(str(task_path))

    assert exc.value.code == 1
    assert "contains invalid JSON" in capsys.readouterr().err


def test_load_task_card_rejects_unreadable_file(tmp_path: Path, monkeypatch, capsys) -> None:
    task_path = tmp_path / "task_input.json"
    task_path.write_text("{}", encoding="utf-8")
    original_read_text = orchestrator.Path.read_text

    def _fake_read_text(self_path: Path, *args, **kwargs):
        if self_path == task_path:
            raise OSError("permission denied")
        return original_read_text(self_path, *args, **kwargs)

    monkeypatch.setattr(orchestrator.Path, "read_text", _fake_read_text)

    with pytest.raises(SystemExit) as exc:
        orchestrator._load_task_card(str(task_path))

    assert exc.value.code == 1
    assert "unable to read task card" in capsys.readouterr().err


def test_load_task_card_rejects_non_dict_json(tmp_path: Path, capsys) -> None:
    task_path = tmp_path / "task_input.json"
    task_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        orchestrator._load_task_card(str(task_path))

    assert exc.value.code == 1
    assert "task card must be a JSON object" in capsys.readouterr().err


def test_single_round_approved_summary_includes_round_details(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "summary details test"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_AWAITING_WORK,
                "round": 1,
                "task_id": "T-604",
                "base_sha": "base-sha",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    work_report = {
        "task_id": "T-604",
        "round": 1,
        "head_sha": "head-sha",
        "files_changed": ["tools/orchestrator.py"],
        "tests": [
            {"name": "pytest", "result": "pass"},
            {"name": "compile", "result": "fail"},
        ],
        "notes": "worker note",
    }
    review_report = {
        "task_id": "T-604",
        "round": 1,
        "decision": "approve",
        "blocking_issues": [],
        "non_blocking_suggestions": ["nb"],
    }

    def fake_wait(path: Path, description: str, **kwargs) -> dict | None:
        _ = (description, kwargs)
        if path == orchestrator.WORK_REPORT:
            return work_report
        if path == orchestrator.REVIEW_REPORT:
            return review_report
        return None

    monkeypatch.setattr(orchestrator, "_wait_for_file", fake_wait)
    monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}->{head}")
    monkeypatch.setattr(orchestrator, "_log_oneline", lambda base, head: f"log {base}->{head}")

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=True,
        round_num=1,
    )

    summary = json.loads((orchestrator.LOOP_DIR / "summary.json").read_text(encoding="utf-8"))
    assert "round_details" in summary
    assert len(summary["round_details"]) == 1
    detail = summary["round_details"][0]
    assert detail["round"] == 1
    assert detail["worker_notes"] == "worker note"
    assert detail["review_decision"] == "approve"
    assert detail["tests_summary"]["total"] == 2
    assert detail["tests_summary"]["pass"] == 1
    assert detail["tests_summary"]["fail"] == 1


def test_single_round_status_writeback_transitions_and_source_sync(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "status writeback", "status": "todo"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_AWAITING_WORK,
                "round": 1,
                "task_id": "T-604",
                "base_sha": "base-sha",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    work_report = {
        "task_id": "T-604",
        "round": 1,
        "head_sha": "head-sha",
        "files_changed": ["src/loop_kit/orchestrator.py"],
        "tests": [],
        "notes": "ok",
    }
    review_report = {
        "task_id": "T-604",
        "round": 1,
        "decision": "approve",
        "blocking_issues": [],
        "non_blocking_suggestions": [],
    }
    observed: list[tuple[str, str | None, str | None]] = []

    def fake_wait_for_role_result(
        *,
        role: str,
        artifact_path: Path,
        config: orchestrator.RunConfig,
        task_id: str,
        round_num: int,
    ) -> dict | None:
        _ = (artifact_path, config, task_id, round_num)
        bus_payload = json.loads(orchestrator.TASK_CARD.read_text(encoding="utf-8"))
        src_payload = json.loads(task_path.read_text(encoding="utf-8"))
        observed.append((role, bus_payload.get("status"), src_payload.get("status")))
        if role == "worker":
            return work_report
        if role == "reviewer":
            return review_report
        return None

    monkeypatch.setattr(orchestrator, "_auto_dispatch_role", lambda **kwargs: None)
    monkeypatch.setattr(orchestrator, "_wait_for_role_result", fake_wait_for_role_result)
    monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}->{head}")
    monkeypatch.setattr(orchestrator, "_log_oneline", lambda base, head: f"log {base}->{head}")

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=True,
        round_num=1,
    )

    assert observed[0] == ("worker", "in_progress", "in_progress")
    assert observed[1] == ("reviewer", "in_progress", "in_progress")
    bus_after = json.loads(orchestrator.TASK_CARD.read_text(encoding="utf-8"))
    src_after = json.loads(task_path.read_text(encoding="utf-8"))
    assert bus_after["status"] == "done"
    assert src_after["status"] == "done"


def test_single_round_dispatch_failure_marks_task_status_blocked(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "dispatch failure", "status": "todo"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_AWAITING_WORK,
                "round": 1,
                "task_id": "T-604",
                "base_sha": "base-sha",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fail_dispatch(**kwargs):
        _ = kwargs
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(orchestrator, "_auto_dispatch_role", fail_dispatch)

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_run(
            _run_config(str(task_path)),
            single_round=True,
            round_num=1,
        )

    assert exc.value.code == 3
    state = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
    assert state["outcome"] == "worker_dispatch_failed"
    bus_after = json.loads(orchestrator.TASK_CARD.read_text(encoding="utf-8"))
    src_after = json.loads(task_path.read_text(encoding="utf-8"))
    assert bus_after["status"] == "blocked"
    assert src_after["status"] == "blocked"


def test_outer_loop_spawns_single_round_subprocess(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "spawn test"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")

    calls: list[list[str]] = []

    def fake_subprocess_popen(cmd, **kwargs):
        _ = kwargs
        calls.append(cmd)
        round_num = int(cmd[cmd.index("--round") + 1])
        orchestrator.REVIEW_REPORT.write_text(
            json.dumps(
                {
                    "task_id": "T-604",
                    "round": round_num,
                    "decision": "approve",
                    "blocking_issues": [],
                    "non_blocking_suggestions": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state = orchestrator._load_state()
        state["state"] = orchestrator.STATE_DONE
        state["outcome"] = "approved"
        state["head_sha"] = "head-sha"
        state["round"] = round_num
        orchestrator._save_state(state)
        return _FakeProc(stdout_lines=[])

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_subprocess_popen)

    orchestrator.cmd_run(
        _run_config(
            str(task_path),
            timeout=30,
            require_heartbeat=True,
            heartbeat_ttl=40,
            auto_dispatch=True,
            dispatch_backend=orchestrator.DISPATCH_BACKEND_NATIVE,
            worker_backend=orchestrator.BACKEND_CODEX,
            reviewer_backend=orchestrator.BACKEND_CLAUDE,
            dispatch_timeout=120,
            artifact_timeout=55,
        ),
        single_round=False,
        round_num=None,
    )

    assert len(calls) == 1
    cmd = calls[0]
    assert "--single-round" in cmd
    assert cmd[cmd.index("--round") + 1] == "1"
    assert "--auto-dispatch" in cmd
    assert cmd[cmd.index("--dispatch-backend") + 1] == orchestrator.DISPATCH_BACKEND_NATIVE
    assert cmd[cmd.index("--worker-backend") + 1] == orchestrator.BACKEND_CODEX
    assert cmd[cmd.index("--reviewer-backend") + 1] == orchestrator.BACKEND_CLAUDE
    assert "--par-bin" not in cmd
    assert "--par-worker-target" not in cmd
    assert "--par-reviewer-target" not in cmd
    assert "--require-heartbeat" in cmd


def test_outer_loop_approved_status_is_written_back_to_source_task_card(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "outer status sync", "status": "todo"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")

    observed: dict[str, tuple[str | None, str | None]] = {}

    def fake_subprocess_popen(cmd, **kwargs):
        _ = kwargs
        round_num = int(cmd[cmd.index("--round") + 1])
        bus_payload = json.loads(orchestrator.TASK_CARD.read_text(encoding="utf-8"))
        src_payload = json.loads(task_path.read_text(encoding="utf-8"))
        observed["during_subprocess"] = (bus_payload.get("status"), src_payload.get("status"))
        orchestrator.REVIEW_REPORT.write_text(
            json.dumps(
                {
                    "task_id": "T-604",
                    "round": round_num,
                    "decision": "approve",
                    "blocking_issues": [],
                    "non_blocking_suggestions": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state = orchestrator._load_state()
        state["state"] = orchestrator.STATE_DONE
        state["outcome"] = "approved"
        state["head_sha"] = "head-sha"
        state["round"] = round_num
        orchestrator._save_state(state)
        return _FakeProc(stdout_lines=[])

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_subprocess_popen)

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=False,
        round_num=None,
    )

    assert observed["during_subprocess"] == ("in_progress", "in_progress")
    bus_after = json.loads(orchestrator.TASK_CARD.read_text(encoding="utf-8"))
    src_after = json.loads(task_path.read_text(encoding="utf-8"))
    assert bus_after["status"] == "done"
    assert src_after["status"] == "done"


def test_outer_loop_streams_single_round_subprocess_stdout_in_real_time(tmp_path: Path, monkeypatch, capsys) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-612", "goal": "stream subprocess output"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)

    printed: list[str] = []
    real_print = builtins.print

    def spy_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        printed.append(sep.join(str(arg) for arg in args) + end)
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", spy_print)

    class _AssertingPipe:
        def __init__(self, lines: list[str]) -> None:
            self._lines = lines
            self._index = 0

        def __iter__(self):
            return self

        def __next__(self) -> str:
            if self._index == 1:
                assert any("__line_1__" in entry for entry in printed), (
                    "first stdout line was not forwarded before reading next line"
                )
            if self._index >= len(self._lines):
                raise StopIteration
            line = self._lines[self._index]
            self._index += 1
            return line

        def close(self) -> None:
            return None

    def fake_subprocess_popen(cmd, **kwargs):
        _ = kwargs
        round_num = int(cmd[cmd.index("--round") + 1])
        orchestrator.REVIEW_REPORT.write_text(
            json.dumps(
                {
                    "task_id": "T-612",
                    "round": round_num,
                    "decision": "approve",
                    "blocking_issues": [],
                    "non_blocking_suggestions": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state = orchestrator._load_state()
        state["state"] = orchestrator.STATE_DONE
        state["outcome"] = "approved"
        state["head_sha"] = "head-sha"
        state["round"] = round_num
        orchestrator._save_state(state)
        proc = _FakeProc(stdout_lines=[])
        proc.stdout = _AssertingPipe(["__line_1__\n", "__line_2__\n"])
        return proc

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_subprocess_popen)

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=False,
        round_num=None,
    )

    out = capsys.readouterr().out
    assert "__line_1__" in out
    assert "__line_2__" in out


def test_outer_loop_terminates_live_subprocess_when_stream_collection_raises(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-614", "goal": "cleanup on stream failure"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)

    proc = _FakeProc(stdout_lines=[], poll_ready_after=999_999)
    monkeypatch.setattr(orchestrator.subprocess, "Popen", lambda cmd, **kwargs: proc)

    def fake_collect_streamed_text_output(*args, **kwargs):
        _ = (args, kwargs)
        raise RuntimeError("stream read failed")

    monkeypatch.setattr(orchestrator, "_collect_streamed_text_output", fake_collect_streamed_text_output)

    with pytest.raises(RuntimeError, match="stream read failed"):
        orchestrator.cmd_run(
            _run_config(str(task_path)),
            single_round=False,
            round_num=None,
        )

    assert proc.terminate_called is True
    assert proc.wait_called is True
    assert proc.wait_timeouts == [2]


def test_outer_loop_cleans_live_subprocess_between_rounds(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-614", "goal": "cleanup between rounds"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")
    monkeypatch.setattr(orchestrator, "_log", lambda msg: None)
    monkeypatch.setattr(orchestrator, "_collect_streamed_text_output", lambda *args, **kwargs: ("", "", 0))

    procs = [
        _FakeProc(stdout_lines=[], poll_ready_after=999_999),
        _FakeProc(stdout_lines=[], poll_ready_after=999_999),
    ]
    popen_calls: list[list[str]] = []

    def fake_subprocess_popen(cmd, **kwargs):
        _ = kwargs
        popen_calls.append(cmd)
        round_num = int(cmd[cmd.index("--round") + 1])
        state = orchestrator._load_state()
        if round_num == 1:
            state["state"] = orchestrator.STATE_AWAITING_WORK
            state["round"] = 2
        else:
            state["state"] = orchestrator.STATE_DONE
            state["outcome"] = "approved"
            state["head_sha"] = "head-sha"
            state["round"] = round_num
        orchestrator._save_state(state)
        return procs[len(popen_calls) - 1]

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_subprocess_popen)

    orchestrator.cmd_run(
        _run_config(str(task_path), max_rounds=2),
        single_round=False,
        round_num=None,
    )

    assert len(popen_calls) == 2
    assert procs[0].terminate_called is True
    assert procs[0].wait_called is True
    assert procs[0].wait_timeouts == [2]


def test_outer_loop_uses_state_as_contract(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "state contract test"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")
    orchestrator.REVIEW_REPORT.write_text(
        json.dumps(
            {
                "task_id": "old-task",
                "round": 99,
                "decision": "changes_required",
                "blocking_issues": [{"id": "OLD"}],
                "non_blocking_suggestions": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def fake_subprocess_popen(cmd, **kwargs):
        _ = kwargs
        calls.append(cmd)
        state = orchestrator._load_state()
        state["state"] = orchestrator.STATE_DONE
        state["outcome"] = "approved"
        state["head_sha"] = "head-sha"
        orchestrator._save_state(state)
        return _FakeProc(stdout_lines=[])

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_subprocess_popen)

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=False,
        round_num=None,
    )

    assert len(calls) == 1
    state = orchestrator._load_state()
    assert state["outcome"] == "approved"


def test_outer_loop_continues_from_state_without_fresh_review_report(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-604", "goal": "state-only progression"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_current_sha", lambda: "base-sha")

    calls: list[list[str]] = []

    def fake_subprocess_popen(cmd, **kwargs):
        _ = kwargs
        calls.append(cmd)
        round_num = int(cmd[cmd.index("--round") + 1])
        state = orchestrator._load_state()
        if round_num == 1:
            state["state"] = orchestrator.STATE_AWAITING_WORK
            state["round"] = 2
            orchestrator._save_state(state)
            # stale/no-review path: do not write review_report for this round
            orchestrator.REVIEW_REPORT.unlink(missing_ok=True)
            return _FakeProc(stdout_lines=[])

        state["state"] = orchestrator.STATE_DONE
        state["outcome"] = "approved"
        state["head_sha"] = "head-sha"
        state["round"] = round_num
        orchestrator._save_state(state)
        return _FakeProc(stdout_lines=[])

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_subprocess_popen)

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=False,
        round_num=None,
    )

    assert len(calls) == 2
    state = orchestrator._load_state()
    assert state["state"] == orchestrator.STATE_DONE
    assert state["outcome"] == "approved"


def test_cmd_run_resume_uses_existing_state_contract(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-608", "goal": "resume state contract"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.TASK_CARD.write_text(task_path.read_text(encoding="utf-8"), encoding="utf-8")
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_AWAITING_WORK,
                "round": 2,
                "task_id": "T-608",
                "base_sha": "base-sha",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_outer(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(orchestrator, "_run_multi_round_via_subprocess", fake_outer)

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=False,
        round_num=None,
        resume=True,
    )

    resume_state = captured.get("resume_from_state")
    assert isinstance(resume_state, dict)
    assert resume_state["task_id"] == "T-608"
    assert resume_state["round"] == 2
    assert resume_state["base_sha"] == "base-sha"


def test_cmd_run_resume_done_approved_exits_cleanly(tmp_path: Path, monkeypatch, capsys) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-608", "goal": "resume done"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {"state": orchestrator.STATE_DONE, "outcome": "approved", "task_id": "T-608"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    called = {"outer": False}
    monkeypatch.setattr(
        orchestrator,
        "_run_multi_round_via_subprocess",
        lambda **kwargs: called.update({"outer": True}),
    )

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=False,
        round_num=None,
        resume=True,
    )

    assert called["outer"] is False
    out = capsys.readouterr().out
    assert "done/approved" in out


def test_cmd_run_resume_failed_state_prints_error_and_exits_3(tmp_path: Path, monkeypatch, capsys) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    task_path = tmp_path / "task_input.json"
    task_path.write_text(
        json.dumps({"task_id": "T-608", "goal": "resume failed"}, ensure_ascii=False),
        encoding="utf-8",
    )
    orchestrator.STATE_FILE.write_text(
        json.dumps(
            {
                "state": orchestrator.STATE_DONE,
                "outcome": "worker_timeout",
                "task_id": "T-608",
                "error": "worker heartbeat stale",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_run(
            _run_config(str(task_path)),
            single_round=False,
            round_num=None,
            resume=True,
        )

    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert "cannot resume" in err
    assert "Re-run without --resume" in err
    assert "worker heartbeat stale" in err


def test_cmd_run_exits_5_when_run_lock_is_unavailable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_acquire_run_lock",
        lambda: (_ for _ in ()).throw(RuntimeError("another orchestrator instance is already running")),
    )

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_run(
            _run_config(".loop/task_card.json"),
            single_round=False,
            round_num=None,
        )

    assert exc.value.code == 5
    err = capsys.readouterr().err
    assert "already running" in err


# ── pure utility functions ──────────────────────────────────────────


class TestParsePorcelainPath:
    def test_plain_path(self) -> None:
        assert orchestrator._parse_porcelain_path("  src/main.py  ") == "src/main.py"

    def test_rename_arrow(self) -> None:
        assert orchestrator._parse_porcelain_path("old.py -> new.py") == "new.py"

    def test_quoted_path(self) -> None:
        assert orchestrator._parse_porcelain_path('"path with spaces.py"') == "path with spaces.py"

    def test_backslash_to_forward_slash(self) -> None:
        assert orchestrator._parse_porcelain_path("src\\nested\\file.py") == "src/nested/file.py"

    def test_quoted_rename(self) -> None:
        assert orchestrator._parse_porcelain_path('"old name.py" -> "new name.py"') == "new name.py"


class TestDisplayPath:
    def test_relative_to_root(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
        result = orchestrator._display_path(tmp_path / "src" / "main.py")
        assert result == "src/main.py"

    def test_outside_root_falls_back_to_absolute(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
        result = orchestrator._display_path(Path("/other/location/file.py"))
        assert "other" in result


class TestNormalizedAbs:
    def test_returns_normalized_string(self, tmp_path: Path) -> None:
        result = orchestrator._normalized_abs(tmp_path / "file.py")
        assert isinstance(result, str)
        assert "file.py" in result


class TestReadJsonIfExists:
    def test_returns_parsed_json(self, tmp_path: Path) -> None:
        p = tmp_path / "data.json"
        p.write_text('{"key": "value"}', encoding="utf-8")
        assert orchestrator._read_json_if_exists(p) == {"key": "value"}

    def test_returns_none_on_missing(self, tmp_path: Path) -> None:
        assert orchestrator._read_json_if_exists(tmp_path / "nope.json") is None

    def test_returns_none_on_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        assert orchestrator._read_json_if_exists(p) is None


class TestReadTextOptional:
    def test_returns_content(self, tmp_path: Path) -> None:
        p = tmp_path / "file.txt"
        p.write_text("hello", encoding="utf-8")
        assert orchestrator._read_text_optional(p) == "hello"

    def test_returns_none_on_missing(self, tmp_path: Path) -> None:
        assert orchestrator._read_text_optional(tmp_path / "nope.txt") is None


class TestAsPromptList:
    def test_list_items(self) -> None:
        result = orchestrator._as_prompt_list(["a", "b", "c"])
        assert result == "- a\n- b\n- c"

    def test_empty_list(self) -> None:
        assert orchestrator._as_prompt_list([]) == "- <none>"

    def test_non_list(self) -> None:
        assert orchestrator._as_prompt_list("not a list") == "- <none>"

    def test_none(self) -> None:
        assert orchestrator._as_prompt_list(None) == "- <none>"


class TestTruncateSummaryText:
    def test_short_text_unchanged(self) -> None:
        assert orchestrator._truncate_summary_text("hello world") == "hello world"

    def test_long_text_truncated(self) -> None:
        text = "a" * 200
        result = orchestrator._truncate_summary_text(text)
        assert len(result) == 120
        assert result.endswith("...")

    def test_whitespace_normalized(self) -> None:
        assert orchestrator._truncate_summary_text("  a   b   c  ") == "a b c"


class TestTs:
    def test_format_matches_iso8601(self) -> None:
        result = orchestrator._ts()
        assert result.endswith("Z")
        assert "T" in result
        # verify it parses as a valid timestamp
        from datetime import datetime

        datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ")


class TestTestsSummary:
    def test_pass_fail_other(self) -> None:
        tests = [
            {"result": "pass"},
            {"result": "pass"},
            {"result": "fail"},
            {"result": "error"},
            {"result": "skip"},
        ]
        s = orchestrator._tests_summary(tests)
        assert s == {"total": 5, "pass": 2, "fail": 2, "other": 1}

    def test_non_list_input(self) -> None:
        s = orchestrator._tests_summary(None)
        assert s == {"total": 0, "pass": 0, "fail": 0, "other": 0}

    def test_empty_list(self) -> None:
        s = orchestrator._tests_summary([])
        assert s == {"total": 0, "pass": 0, "fail": 0, "other": 0}

    def test_failed_variant(self) -> None:
        s = orchestrator._tests_summary([{"result": "failed"}])
        assert s["fail"] == 1


# ── streaming / parsing ─────────────────────────────────────────────


class TestExtractCodexThreadId:
    def test_finds_thread_started(self) -> None:
        stdout = '{"type":"thread.started","thread_id":"tid_123"}\n{"type":"item.completed","item":{}}\n'
        assert orchestrator._extract_codex_thread_id(stdout) == "tid_123"

    def test_returns_none_on_no_match(self) -> None:
        stdout = '{"type":"item.completed"}\n'
        assert orchestrator._extract_codex_thread_id(stdout) is None

    def test_ignores_malformed_json(self) -> None:
        stdout = "not json\n{}\n"
        assert orchestrator._extract_codex_thread_id(stdout) is None


class TestExtractOpencodeSessionId:
    def test_finds_session_from_step_start(self) -> None:
        stdout = '{"type":"step_start","part":{"sessionID":"sess_abc123"}}\n{"type":"text","part":{"text":"hello"}}\n'
        assert orchestrator._extract_opencode_session_id(stdout) == "sess_abc123"

    def test_returns_none_on_no_step_start(self) -> None:
        stdout = '{"type":"text","part":{"text":"hello"}}\n'
        assert orchestrator._extract_opencode_session_id(stdout) is None

    def test_ignores_malformed_json(self) -> None:
        stdout = "not json\n{}\n"
        assert orchestrator._extract_opencode_session_id(stdout) is None

    def test_strips_whitespace_from_session_id(self) -> None:
        stdout = '{"type":"step_start","part":{"sessionID":"  sess_ws  "}}\n'
        assert orchestrator._extract_opencode_session_id(stdout) == "sess_ws"

    def test_returns_none_on_empty_session_id(self) -> None:
        stdout = '{"type":"step_start","part":{"sessionID":""}}\n'
        assert orchestrator._extract_opencode_session_id(stdout) is None

    def test_returns_none_when_part_not_dict(self) -> None:
        stdout = '{"type":"step_start","part":"not a dict"}\n'
        assert orchestrator._extract_opencode_session_id(stdout) is None


class TestFlattenTextPayload:
    def test_string(self) -> None:
        assert orchestrator._flatten_text_payload("  hello  ") == "hello"

    def test_list(self) -> None:
        assert orchestrator._flatten_text_payload(["a", "b"]) == "a b"

    def test_nested_dict_text_key(self) -> None:
        assert orchestrator._flatten_text_payload({"text": "value"}) == "value"

    def test_nested_dict_message_key(self) -> None:
        assert orchestrator._flatten_text_payload({"message": "val"}) == "val"

    def test_empty_dict(self) -> None:
        assert orchestrator._flatten_text_payload({}) == ""

    def test_none(self) -> None:
        assert orchestrator._flatten_text_payload(None) == ""


class TestExtractCommandSummary:
    def test_string_command(self) -> None:
        result = orchestrator._extract_command_summary({"command": "npm test"})
        assert result == "npm test"

    def test_list_command(self) -> None:
        result = orchestrator._extract_command_summary({"command": ["python", "-m", "pytest"]})
        assert "python" in result
        assert "pytest" in result

    def test_call_dict_fallback(self) -> None:
        result = orchestrator._extract_command_summary({"call": {"command": "make build"}})
        assert result == "make build"

    def test_empty_returns_empty(self) -> None:
        assert orchestrator._extract_command_summary({}) == ""


class TestCodexEventSummary:
    def test_command_execution(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "npm test"},
            }
        )
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert result == "[worker] Running: npm test"

    def test_agent_message(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "I fixed the bug."},
            }
        )
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert "[worker] Message:" in result
        assert "I fixed the bug." in result

    def test_file_change(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [
                        {"path": "src/loop_kit/orchestrator.py"},
                        {"path": "tests/test_orchestrator.py"},
                    ],
                },
            }
        )
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert result == "[worker] Editing: orchestrator.py, test_orchestrator.py"

    def test_top_level_file_change_uses_short_filename(self) -> None:
        line = json.dumps(
            {
                "type": "file_change",
                "changes": [
                    {"path": "src/loop_kit/orchestrator.py"},
                ],
            }
        )
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert result == "[worker] Editing: orchestrator.py"

    def test_file_change_keeps_colliding_basenames(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [
                        {"path": "src/api/config.py"},
                        {"path": "tests/config.py"},
                    ],
                },
            }
        )
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert result == "[worker] Editing: api/config.py, tests/config.py"

    def test_command_execution_strips_powershell_wrapper(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": (
                        '"C:\\Program Files\\PowerShell\\7\\pwsh.exe" '
                        "-Command Get-Content -Path src/loop_kit/orchestrator.py "
                        "| Select-Object -Skip 40 -First 20"
                    ),
                },
            }
        )
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert result == (
            "[worker] Running: Get-Content -Path src/loop_kit/orchestrator.py | Select-Object -Skip 40 -First 20"
        )

    def test_item_started(self) -> None:
        line = json.dumps(
            {
                "type": "item.started",
                "item": {"type": "command_execution"},
            }
        )
        assert orchestrator._codex_event_summary("worker", "codex", line) is None

    def test_non_codex_returns_none(self) -> None:
        line = json.dumps({"type": "item.completed", "item": {"type": "command_execution"}})
        assert orchestrator._codex_event_summary("worker", "claude", line) is None

    def test_malformed_json_returns_none(self) -> None:
        assert orchestrator._codex_event_summary("worker", "codex", "bad json") is None

    def test_unknown_event_type_returns_none(self) -> None:
        line = json.dumps({"type": "unknown.event"})
        assert orchestrator._codex_event_summary("worker", "codex", line) is None


class TestDispatchFailureHint:
    def test_timeout_hint(self) -> None:
        result = orchestrator._dispatch_failure_hint(backend="codex", stderr="", timeout=True, timeout_sec=45)
        assert "Backend codex timed out. Try increasing --dispatch-timeout (current: 45s)." in result

    def test_auth_hint(self) -> None:
        result = orchestrator._dispatch_failure_hint(backend="codex", stderr="Error: unauthorized 401")
        assert "Authentication failed for codex. Check your API key / token configuration." in result

    def test_auth_token_expired_hint(self) -> None:
        result = orchestrator._dispatch_failure_hint(backend="codex", stderr="Error: auth token expired")
        assert "Authentication failed for codex. Check your API key / token configuration." in result

    def test_not_found_hint(self) -> None:
        result = orchestrator._dispatch_failure_hint(backend="codex", stderr="codex: command not found")
        assert "Backend codex not found. Run `codex --version` to verify installation." in result

    def test_not_recognized_hint(self) -> None:
        result = orchestrator._dispatch_failure_hint(
            backend="codex",
            stderr="'codex' is not recognized as an internal or external command",
        )
        assert "Backend codex not found. Run `codex --version` to verify installation." in result

    def test_rate_limit_hint(self) -> None:
        result = orchestrator._dispatch_failure_hint(backend="codex", stderr="rate limit exceeded (429)")
        assert "codex rate limit hit. Wait a moment or increase --dispatch-timeout." in result

    def test_timeout_hint_from_stderr(self) -> None:
        result = orchestrator._dispatch_failure_hint(
            backend="codex",
            stderr="request timed out",
            timeout_sec=120,
        )
        assert "Backend codex timed out. Try increasing --dispatch-timeout (current: 120s)." in result

    def test_fallback_hint(self) -> None:
        result = orchestrator._dispatch_failure_hint(backend="codex", stderr="unknown error")
        assert "auth/network" in result


# ── dispatch ─────────────────────────────────────────────────────────


class TestWriteDispatchLog:
    def test_writes_structured_log(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(orchestrator, "LOGS_DIR", tmp_path)
        result = subprocess.CompletedProcess(args=["codex"], returncode=0, stdout="out\n", stderr="err\n")
        result.stdout = "out\n"
        result.stderr = "err\n"
        orchestrator._write_dispatch_log("worker", ["codex", "exec"], result, "sid-123")
        log = (tmp_path / "worker_dispatch.log").read_text(encoding="utf-8")
        assert "role=worker" in log
        assert "returncode=0" in log
        assert "session_id=sid-123" in log
        assert "cmd=codex exec" in log
        assert "stdout:" in log
        assert "stderr:" in log

    def test_no_session_id_omits_line(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(orchestrator, "LOGS_DIR", tmp_path)
        result = subprocess.CompletedProcess(args=["codex"], returncode=1, stdout="", stderr="")
        result.stdout = ""
        result.stderr = ""
        orchestrator._write_dispatch_log("worker", ["codex"], result, None)
        log = (tmp_path / "worker_dispatch.log").read_text(encoding="utf-8")
        assert "session_id=" not in log


class TestResolveExeFromCandidates:
    def test_finds_existing_file(self, tmp_path: Path) -> None:
        exe = tmp_path / "mybin"
        exe.write_text("")
        result = orchestrator._resolve_exe_from_candidates(backend="test", candidates=[None, str(exe)])
        assert result == str(exe)

    def test_raises_when_none_found(self) -> None:
        with pytest.raises(RuntimeError, match="Cannot find executable"):
            orchestrator._resolve_exe_from_candidates(backend="test", candidates=[None, "/nonexistent/path"])


class TestParDispatchRemoval:
    def test_dispatch_backend_par_choice_rejected(self, monkeypatch) -> None:
        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "run", "--dispatch-backend", "par"],
        )
        with pytest.raises(SystemExit):
            orchestrator.main()

    def test_par_bin_flag_rejected(self, monkeypatch) -> None:
        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "run", "--par-bin", "par"],
        )
        with pytest.raises(SystemExit):
            orchestrator.main()


# ── locking, heartbeat, polling ─────────────────────────────────────


class TestLoopLock:
    def test_acquire_and_release(self, tmp_path: Path) -> None:
        lock = orchestrator._LoopLock(tmp_path / "test.lock")
        lock.acquire()
        assert lock._handle is not None
        lock.release()
        assert lock._handle is None

    def test_context_manager(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "test.lock"
        with orchestrator._LoopLock(lock_file) as lock:
            assert lock._handle is not None
        assert lock._handle is None

    def test_release_without_acquire_is_noop(self, tmp_path: Path) -> None:
        lock = orchestrator._LoopLock(tmp_path / "test.lock")
        lock.release()  # should not raise

    def test_second_acquire_raises(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "test.lock"
        lock1 = orchestrator._LoopLock(lock_file)
        lock1.acquire()
        lock2 = orchestrator._LoopLock(lock_file)
        with pytest.raises(RuntimeError, match="another orchestrator instance"):
            lock2.acquire()
        lock1.release()


class TestHeartbeatAgeSec:
    def test_returns_age_for_existing_file(self, tmp_path: Path) -> None:
        hb = tmp_path / "hb.json"
        hb.write_text("{}", encoding="utf-8")
        age = orchestrator._heartbeat_age_sec(hb, now=hb.stat().st_mtime + 5.0)
        assert age == pytest.approx(5.0, abs=0.1)

    def test_returns_none_for_missing(self, tmp_path: Path) -> None:
        assert orchestrator._heartbeat_age_sec(tmp_path / "nope.json") is None

    def test_clamps_to_zero(self, tmp_path: Path) -> None:
        hb = tmp_path / "hb.json"
        hb.write_text("{}", encoding="utf-8")
        age = orchestrator._heartbeat_age_sec(hb, now=hb.stat().st_mtime - 10.0)
        assert age == 0.0


class TestRoleIsAlive:
    def test_alive_when_fresh(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        hb = orchestrator._heartbeat_path("worker")
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({"pid": 42}), encoding="utf-8")
        alive, reason = orchestrator._role_is_alive("worker", 30)
        assert alive
        assert "pid=42" in reason

    def test_dead_when_missing(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        alive, reason = orchestrator._role_is_alive("worker", 30)
        assert not alive
        assert "missing" in reason

    def test_dead_when_stale(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        hb = orchestrator._heartbeat_path("worker")
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text("{}", encoding="utf-8")
        # make it old
        import os

        old_time = hb.stat().st_mtime - 100
        os.utime(hb, (old_time, old_time))
        alive, reason = orchestrator._role_is_alive("worker", 30)
        assert not alive
        assert "stale" in reason


class TestWriteTemplateIfMissing:
    def test_creates_new_file(self, tmp_path: Path) -> None:
        p = tmp_path / "new.txt"
        assert orchestrator._write_template_if_missing(p, "content")
        assert p.read_text(encoding="utf-8") == "content"

    def test_skips_existing(self, tmp_path: Path) -> None:
        p = tmp_path / "existing.txt"
        p.write_text("old", encoding="utf-8")
        assert not orchestrator._write_template_if_missing(p, "new")
        assert p.read_text(encoding="utf-8") == "old"


# ── CLI commands and state ──────────────────────────────────────────


class TestValidateReport:
    def test_valid_work_report(self) -> None:
        work = {"task_id": "T-1", "head_sha": "abc", "round": 1}
        assert (
            orchestrator._validate_report(
                work,
                expected_task_id="T-1",
                expected_round=1,
                schema="work_report",
            )
            is None
        )

    def test_valid_review_report(self) -> None:
        review = {"task_id": "T-1", "round": 1, "decision": "approve"}
        assert (
            orchestrator._validate_report(
                review,
                expected_task_id="T-1",
                expected_round=1,
                schema="review_report",
            )
            is None
        )

    def test_work_report_missing_field(self) -> None:
        assert "missing required field" in (
            orchestrator._validate_report(
                {},
                expected_task_id="T-1",
                expected_round=1,
                schema="work_report",
            )
            or ""
        )

    def test_review_report_missing_field(self) -> None:
        assert "missing required field" in (
            orchestrator._validate_report(
                {},
                expected_task_id="T-1",
                expected_round=1,
                schema="review_report",
            )
            or ""
        )

    def test_work_report_wrong_type_int(self) -> None:
        work = {"task_id": "T-1", "head_sha": "abc", "round": "1"}
        assert "must be int" in (
            orchestrator._validate_report(
                work,
                expected_task_id="T-1",
                expected_round=1,
                schema="work_report",
            )
            or ""
        )

    def test_work_report_empty_string(self) -> None:
        work = {"task_id": "  ", "head_sha": "abc", "round": 1}
        assert "non-empty" in (
            orchestrator._validate_report(
                work,
                expected_task_id="T-1",
                expected_round=1,
                schema="work_report",
            )
            or ""
        )

    def test_work_report_task_id_mismatch(self) -> None:
        work = {"task_id": "T-2", "head_sha": "abc", "round": 1}
        assert "mismatch" in (
            orchestrator._validate_report(
                work,
                expected_task_id="T-1",
                expected_round=1,
                schema="work_report",
            )
            or ""
        )

    def test_work_report_round_mismatch(self) -> None:
        work = {"task_id": "T-1", "head_sha": "abc", "round": 2}
        assert "mismatch" in (
            orchestrator._validate_report(
                work,
                expected_task_id="T-1",
                expected_round=1,
                schema="work_report",
            )
            or ""
        )

    def test_review_report_invalid_decision(self) -> None:
        review = {"task_id": "T-1", "round": 1, "decision": "maybe"}
        assert "must be one of" in (
            orchestrator._validate_report(
                review,
                expected_task_id="T-1",
                expected_round=1,
                schema="review_report",
            )
            or ""
        )

    def test_review_report_task_id_mismatch(self) -> None:
        review = {"task_id": "T-2", "round": 1, "decision": "approve"}
        assert "mismatch" in (
            orchestrator._validate_report(
                review,
                expected_task_id="T-1",
                expected_round=1,
                schema="review_report",
            )
            or ""
        )

    def test_work_report_list_fields_must_be_lists(self) -> None:
        work = {"task_id": "T-1", "head_sha": "abc", "round": 1, "files_changed": "not a list"}
        assert "must be a list" in (
            orchestrator._validate_report(
                work,
                expected_task_id="T-1",
                expected_round=1,
                schema="work_report",
            )
            or ""
        )

    def test_unknown_schema_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown schema"):
            orchestrator._validate_report(
                {},
                expected_task_id="T-1",
                expected_round=1,
                schema="unknown",
            )


class TestLoadState:
    def test_default_when_no_file(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        state = orchestrator._load_state()
        assert state["state"] == "idle"
        assert state["round"] == 0

    def test_loads_existing(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.STATE_FILE.write_text(json.dumps({"state": "done", "round": 3}), encoding="utf-8")
        state = orchestrator._load_state()
        assert state["state"] == "done"
        assert state["round"] == 3

    def test_returns_default_when_json_is_corrupted(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.STATE_FILE.write_text("{broken\n", encoding="utf-8")

        state = orchestrator._load_state()

        assert state == {"version": orchestrator.STATE_SCHEMA_VERSION, "state": "idle", "round": 0, "task_id": None}

    def test_returns_default_on_read_oserror(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.STATE_FILE.write_text("{}", encoding="utf-8")
        original_read_text = orchestrator.Path.read_text

        def _fake_read_text(self_path: Path, *args, **kwargs):
            if self_path == orchestrator.STATE_FILE:
                raise OSError("permission denied")
            return original_read_text(self_path, *args, **kwargs)

        monkeypatch.setattr(orchestrator.Path, "read_text", _fake_read_text)

        state = orchestrator._load_state()

        assert state == {"version": orchestrator.STATE_SCHEMA_VERSION, "state": "idle", "round": 0, "task_id": None}

    def test_recovers_from_backup_when_state_is_corrupted(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        backup_state = {"state": "awaiting_review", "round": 7, "task_id": "T-616"}
        orchestrator._STATE_BACKUP.write_text(json.dumps(backup_state), encoding="utf-8")
        orchestrator.STATE_FILE.write_text("{broken\n", encoding="utf-8")

        state = orchestrator._load_state()

        assert state == {"version": orchestrator.STATE_SCHEMA_VERSION, **backup_state}
        assert json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8")) == {
            "version": orchestrator.STATE_SCHEMA_VERSION,
            **backup_state,
        }
        assert "state.json corrupted, recovered from backup" in capsys.readouterr().err

    def test_recovers_from_backup_on_read_oserror(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        backup_state = {"state": "done", "round": 3, "task_id": "T-616"}
        orchestrator._STATE_BACKUP.write_text(json.dumps(backup_state), encoding="utf-8")
        orchestrator.STATE_FILE.write_text("{}", encoding="utf-8")
        original_read_text = orchestrator.Path.read_text

        def _fake_read_text(self_path: Path, *args, **kwargs):
            if self_path == orchestrator.STATE_FILE:
                raise OSError("permission denied")
            return original_read_text(self_path, *args, **kwargs)

        monkeypatch.setattr(orchestrator.Path, "read_text", _fake_read_text)

        state = orchestrator._load_state()

        assert state == {"version": orchestrator.STATE_SCHEMA_VERSION, **backup_state}
        assert json.loads(original_read_text(orchestrator.STATE_FILE, encoding="utf-8")) == {
            "version": orchestrator.STATE_SCHEMA_VERSION,
            **backup_state,
        }
        assert "state.json corrupted, recovered from backup" in capsys.readouterr().err


class TestSaveState:
    def test_writes_json(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator._save_state({"state": "done", "round": 2})
        data = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
        assert data["version"] == orchestrator.STATE_SCHEMA_VERSION
        assert data["state"] == "done"
        assert data["round"] == 2

    def test_creates_backup_before_overwriting_state(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        previous_state = {"state": "awaiting_work", "round": 1}
        orchestrator.STATE_FILE.write_text(json.dumps(previous_state), encoding="utf-8")

        orchestrator._save_state({"state": "done", "round": 2})

        assert json.loads(orchestrator._STATE_BACKUP.read_text(encoding="utf-8")) == previous_state
        assert json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8")) == {
            "version": orchestrator.STATE_SCHEMA_VERSION,
            "state": "done",
            "round": 2,
            "task_id": None,
        }


class TestArchiveTaskSummary:
    def test_archives_existing_summary(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        summary = orchestrator.LOOP_DIR / "summary.json"
        summary.write_text('{"outcome": "approved"}', encoding="utf-8")
        dest = orchestrator._archive_task_summary("T-1")
        assert dest is not None
        assert dest.exists()
        assert dest.name == "summary.json"

    def test_returns_none_when_missing(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        assert orchestrator._archive_task_summary("T-1") is None


class TestCmdStatus:
    def test_shows_human_readable_summary(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.STATE_FILE.write_text(json.dumps({"state": "done", "round": 1}), encoding="utf-8")
        orchestrator.TASK_CARD.write_text("{}", encoding="utf-8")
        orchestrator.cmd_status()
        out = capsys.readouterr().out
        assert "State: done" in out
        assert "Round: 1" in out
        assert "task_card.json: EXISTS" in out
        assert "work_report.json: missing" in out

    def test_shows_context_file_stats(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        context_dir = tmp_path / ".loop" / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "project_facts.md").write_text(
            "# facts\n- single-file rule\n- subprocess-per-round\n",
            encoding="utf-8",
        )
        (context_dir / "pitfalls.md").write_text("# pitfalls\n- stale lock\n", encoding="utf-8")
        (context_dir / "patterns.jsonl").write_text(
            json.dumps(
                {
                    "pattern": "fresh pattern",
                    "category": "workflow",
                    "confidence": 0.9,
                    "last_verified": "2026-04-01T12:00:00Z",
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "pattern": "stale pattern",
                    "category": "workflow",
                    "confidence": 0.9,
                    "last_verified": "2025-01-01T00:00:00Z",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        orchestrator.cmd_status()
        out = capsys.readouterr().out

        assert "Context files:" in out
        assert "project_facts.md: EXISTS (facts=2)" in out
        assert "pitfalls.md: EXISTS (pitfalls=1)" in out
        assert "patterns.jsonl: EXISTS (entries=2, high_confidence=1, stale=1)" in out


class TestCmdHealth:
    def test_reports_both_roles(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.cmd_health(30)
        out = capsys.readouterr().out
        assert "worker:" in out
        assert "reviewer:" in out
        assert "dead" in out  # no heartbeats exist


class TestDispatchMetricsReport:
    def test_collect_and_summarize_mixed_partial_metrics(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        feed_entries = [
            {
                "event": orchestrator.FEED_DISPATCH_PHASE_METRICS,
                "data": {
                    "task_id": "T-716",
                    "role": "worker",
                    "startup_ms": 100,
                    "context_to_work_ms": 300,
                    "work_to_artifact_ms": 500,
                    "total_ms": 600,
                    "read_ms": 200,
                    "search_ms": 100,
                    "edit_ms": 150,
                    "test_ms": 50,
                    "unknown_ms": 0,
                },
            },
            {
                "event": orchestrator.FEED_DISPATCH_PHASE_METRICS,
                "data": {
                    "task_id": "T-716",
                    "role": "worker",
                    "startup_ms": 140,
                    "context_to_work_ms": None,
                    "work_to_artifact_ms": None,
                    "total_ms": 700,
                    "read_ms": None,
                    "search_ms": None,
                    "edit_ms": 220,
                    "test_ms": None,
                    "unknown_ms": 80,
                },
            },
            {
                "event": orchestrator.FEED_DISPATCH_PHASE_METRICS,
                "data": {
                    "task_id": "T-716",
                    "role": "worker",
                    "startup_ms": 200,
                    "context_to_work_ms": 300,
                    "work_to_artifact_ms": 500,
                    "total_ms": 800,
                    "read_ms": 240,
                    "search_ms": 60,
                    "edit_ms": 200,
                    "test_ms": 90,
                    "unknown_ms": None,
                },
            },
            {
                "event": orchestrator.FEED_DISPATCH_PHASE_METRICS,
                "data": {
                    "task_id": "T-716",
                    "role": "reviewer",
                    "startup_ms": 50,
                    "context_to_work_ms": 20,
                    "work_to_artifact_ms": 70,
                    "total_ms": 140,
                    "read_ms": 10,
                    "search_ms": 5,
                    "edit_ms": 40,
                    "test_ms": 15,
                    "unknown_ms": 0,
                },
            },
            {"event": "dispatch_start", "data": {"task_id": "T-716", "role": "worker"}},
        ]
        feed_payload = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in feed_entries)
        feed_payload += "{broken json line\n"
        (orchestrator.LOGS_DIR / "feed.jsonl").write_text(feed_payload, encoding="utf-8")

        rows = orchestrator._collect_dispatch_phase_metrics_events(
            orchestrator.LOGS_DIR / "feed.jsonl",
            task_id="T-716",
            role="worker",
        )
        assert len(rows) == 3

        summary = orchestrator._summarize_dispatch_phase_metrics(rows)
        assert summary["startup_ms"]["count"] == 3
        assert summary["startup_ms"]["missing"] == 0
        assert summary["startup_ms"]["avg"] == pytest.approx(146.666, rel=1e-3)
        assert summary["startup_ms"]["p50"] == 140
        assert summary["startup_ms"]["p95"] == 200

        assert summary["context_to_work_ms"]["count"] == 2
        assert summary["context_to_work_ms"]["missing"] == 1
        assert summary["context_to_work_ms"]["avg"] == 300
        assert summary["context_to_work_ms"]["p50"] == 300
        assert summary["context_to_work_ms"]["p95"] == 300

        assert summary["work_to_artifact_ms"]["count"] == 2
        assert summary["work_to_artifact_ms"]["missing"] == 1
        assert summary["total_ms"]["count"] == 3
        assert summary["total_ms"]["missing"] == 0
        assert summary["total_ms"]["p50"] == 700
        assert summary["total_ms"]["p95"] == 800

        subphase_summary = orchestrator._summarize_dispatch_subphase_metrics(rows)
        assert subphase_summary["read_ms"]["count"] == 2
        assert subphase_summary["read_ms"]["missing"] == 1
        assert subphase_summary["read_ms"]["avg"] == 220
        assert subphase_summary["read_ms"]["p50"] == 200
        assert subphase_summary["read_ms"]["p95"] == 240

        assert subphase_summary["search_ms"]["count"] == 2
        assert subphase_summary["search_ms"]["missing"] == 1
        assert subphase_summary["search_ms"]["avg"] == 80
        assert subphase_summary["search_ms"]["p50"] == 60
        assert subphase_summary["search_ms"]["p95"] == 100

        assert subphase_summary["edit_ms"]["count"] == 3
        assert subphase_summary["edit_ms"]["missing"] == 0
        assert subphase_summary["edit_ms"]["avg"] == pytest.approx(190.0)
        assert subphase_summary["edit_ms"]["p50"] == 200
        assert subphase_summary["edit_ms"]["p95"] == 220

        assert subphase_summary["test_ms"]["count"] == 2
        assert subphase_summary["test_ms"]["missing"] == 1
        assert subphase_summary["test_ms"]["avg"] == 70
        assert subphase_summary["test_ms"]["p50"] == 50
        assert subphase_summary["test_ms"]["p95"] == 90

        assert subphase_summary["unknown_ms"]["count"] == 2
        assert subphase_summary["unknown_ms"]["missing"] == 1
        assert subphase_summary["unknown_ms"]["avg"] == 40
        assert subphase_summary["unknown_ms"]["p50"] == 0
        assert subphase_summary["unknown_ms"]["p95"] == 80

    def test_cli_dispatch_metrics_filters_task_id_and_role(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        feed_entries = [
            {
                "event": orchestrator.FEED_DISPATCH_PHASE_METRICS,
                "data": {
                    "task_id": "T-900",
                    "role": "worker",
                    "startup_ms": 10,
                    "context_to_work_ms": 20,
                    "work_to_artifact_ms": 30,
                    "total_ms": 60,
                    "read_ms": 8,
                    "search_ms": 4,
                    "edit_ms": 18,
                    "test_ms": None,
                    "unknown_ms": 0,
                },
            },
            {
                "event": orchestrator.FEED_DISPATCH_PHASE_METRICS,
                "data": {
                    "task_id": "T-900",
                    "role": "reviewer",
                    "startup_ms": 30,
                    "context_to_work_ms": None,
                    "work_to_artifact_ms": None,
                    "total_ms": 120,
                    "read_ms": 20,
                    "search_ms": 35,
                    "edit_ms": 50,
                    "test_ms": None,
                    "unknown_ms": 15,
                },
            },
            {
                "event": orchestrator.FEED_DISPATCH_PHASE_METRICS,
                "data": {
                    "task_id": "T-901",
                    "role": "reviewer",
                    "startup_ms": 90,
                    "context_to_work_ms": 40,
                    "work_to_artifact_ms": 80,
                    "total_ms": 210,
                    "read_ms": 30,
                    "search_ms": 25,
                    "edit_ms": 20,
                    "test_ms": 5,
                    "unknown_ms": 0,
                },
            },
        ]
        (orchestrator.LOGS_DIR / "feed.jsonl").write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in feed_entries),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "dispatch-metrics", "--task-id", "T-900", "--role", "reviewer"],
        )

        orchestrator.main()
        out = capsys.readouterr().out
        assert "Filters: task_id=T-900 role=reviewer" in out
        assert "Matched dispatch_phase_metrics events: 1" in out
        startup_row = next(line for line in out.splitlines() if line.strip().startswith("startup_ms"))
        startup_cells = [cell.strip() for cell in startup_row.split("|")]
        assert startup_cells == ["startup_ms", "1", "0", "30.0", "30.0", "30.0"]
        context_row = next(line for line in out.splitlines() if line.strip().startswith("context_to_work_ms"))
        context_cells = [cell.strip() for cell in context_row.split("|")]
        assert context_cells == ["context_to_work_ms", "0", "1", "n/a", "n/a", "n/a"]
        assert "Work subphase breakdown (within work_to_artifact)" in out
        read_row = next(line for line in out.splitlines() if line.strip().startswith("read_ms"))
        read_cells = [cell.strip() for cell in read_row.split("|")]
        assert read_cells == ["read_ms", "1", "0", "20.0", "20.0", "20.0"]
        test_row = next(line for line in out.splitlines() if line.strip().startswith("test_ms"))
        test_cells = [cell.strip() for cell in test_row.split("|")]
        assert test_cells == ["test_ms", "0", "1", "n/a", "n/a", "n/a"]

    def test_dispatch_metrics_no_matching_data_is_deterministic(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        (orchestrator.LOGS_DIR / "feed.jsonl").write_text(
            json.dumps(
                {
                    "event": orchestrator.FEED_DISPATCH_PHASE_METRICS,
                    "data": {"task_id": "T-999", "role": "worker", "startup_ms": 15, "total_ms": 100},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        orchestrator.cmd_dispatch_metrics(task_id="T-716", role="reviewer")
        out = capsys.readouterr().out
        assert "Matched dispatch_phase_metrics events: 0" in out
        assert "No matching dispatch_phase_metrics events." in out
        startup_row = next(line for line in out.splitlines() if line.strip().startswith("startup_ms"))
        startup_cells = [cell.strip() for cell in startup_row.split("|")]
        assert startup_cells == ["startup_ms", "0", "0", "n/a", "n/a", "n/a"]
        read_row = next(line for line in out.splitlines() if line.strip().startswith("read_ms"))
        read_cells = [cell.strip() for cell in read_row.split("|")]
        assert read_cells == ["read_ms", "0", "0", "n/a", "n/a", "n/a"]


class TestCmdExtractDiff:
    def test_prints_diff(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}..{head}")
        monkeypatch.setattr(orchestrator, "_is_valid_ref", lambda ref: True)
        orchestrator.cmd_extract_diff("abc", "def")
        assert capsys.readouterr().out.strip() == "diff abc..def"


class TestRestoreTargetNameFromArchive:
    def test_round_prefixed(self) -> None:
        assert orchestrator._restore_target_name_from_archive("r1_state") == "state.json"

    def test_round_prefixed_work_report(self) -> None:
        assert orchestrator._restore_target_name_from_archive("r2_work_report") == "work_report.json"

    def test_summary(self) -> None:
        assert orchestrator._restore_target_name_from_archive("summary") == "summary.json"

    def test_bare_name(self) -> None:
        assert orchestrator._restore_target_name_from_archive("task_card") == "task_card.json"


class TestRegisterBackendValidation:
    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            orchestrator.register_backend(
                "  ",
                lambda e, p: ([], None, None),
                lambda b: b,
                lambda role, backend, line: None,
            )

    def test_strip_and_lower(self, monkeypatch) -> None:
        monkeypatch.setattr(orchestrator, "_resolve_backend_exe", lambda b: "test.exe")
        orchestrator.register_backend(
            "MyBackend",
            lambda e, p: ([e, "run"], None, None),
            lambda b: "test.exe",
            lambda role, backend, line: None,
        )
        assert "mybackend" in orchestrator._available_backends()
        # cleanup
        del orchestrator._BACKEND_REGISTRY["mybackend"]


class TestIsGitRepoRoot:
    def test_true_when_git_exists(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        assert orchestrator._is_git_repo_root(tmp_path)

    def test_true_when_git_is_file(self, tmp_path: Path) -> None:
        # git worktrees and submodules use a .git file, not directory
        (tmp_path / ".git").write_text("gitdir: /some/other/path\n", encoding="utf-8")
        assert orchestrator._is_git_repo_root(tmp_path)

    def test_false_when_no_git(self, tmp_path: Path) -> None:
        assert not orchestrator._is_git_repo_root(tmp_path)


class TestGitHelper:
    def test_passes_timeout_to_subprocess_run(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr(orchestrator.subprocess, "run", _fake_run)
        result = orchestrator._git("status")

        assert result == "ok"
        assert captured["cmd"] == ["git", "-C", str(orchestrator.ROOT), "status"]
        assert captured["timeout"] == 30

    def test_raises_runtime_error_on_timeout(self, monkeypatch) -> None:
        def _fake_run(*args, **kwargs):
            _ = args, kwargs
            raise subprocess.TimeoutExpired(
                cmd=["git", "-C", str(orchestrator.ROOT), "status"],
                timeout=12,
            )

        monkeypatch.setattr(orchestrator.subprocess, "run", _fake_run)
        with pytest.raises(RuntimeError, match=r"git status timed out after 12s"):
            orchestrator._git("status", timeout=12)


class TestFailWithState:
    def test_exits_with_given_code(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(orchestrator, "STATE_FILE", tmp_path / "state.json")
        state = {"state": "idle"}
        with pytest.raises(SystemExit) as exc:
            orchestrator._fail_with_state(state, outcome="test_fail", message="boom", exit_code=42)
        assert exc.value.code == 42

    def test_saves_state(self, monkeypatch, tmp_path) -> None:
        state_file = tmp_path / "state.json"
        monkeypatch.setattr(orchestrator, "STATE_FILE", state_file)
        state = {"state": "idle", "round": 1}
        with pytest.raises(SystemExit):
            orchestrator._fail_with_state(state, outcome="test_fail", message="boom")
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert saved["state"] == orchestrator.STATE_DONE
        assert saved["outcome"] == "test_fail"
        assert saved["error"] == "boom"
        assert "failed_at" in saved


class TestEnforceCleanWorktree:
    def test_clean_tree_passes(self, monkeypatch) -> None:
        monkeypatch.setattr(orchestrator, "_dirty_tracked_paths", lambda: [])
        # Should not raise
        orchestrator._enforce_clean_worktree_or_exit(allow_dirty=False)

    def test_dirty_tree_exits_4(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_dirty_tracked_paths", lambda: ["src/foo.py"])
        with pytest.raises(SystemExit) as exc:
            orchestrator._enforce_clean_worktree_or_exit(allow_dirty=False)
        assert exc.value.code == 4
        assert "dirty git working tree" in capsys.readouterr().err

    def test_dirty_tree_with_allow_dirty_passes(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_dirty_tracked_paths", lambda: ["src/foo.py"])
        orchestrator._enforce_clean_worktree_or_exit(allow_dirty=True)
        assert "Proceeding" in capsys.readouterr().err


class TestDirtyTrackedPaths:
    def test_excludes_untracked(self, monkeypatch) -> None:
        monkeypatch.setattr(orchestrator, "_is_git_repo_root", lambda p: True)
        monkeypatch.setattr(orchestrator, "_git", lambda *a: "?? newfile.py\n M modified.py\n")
        result = orchestrator._dirty_tracked_paths()
        assert result == ["modified.py"]

    def test_excludes_loop_dir(self, monkeypatch) -> None:
        monkeypatch.setattr(orchestrator, "_is_git_repo_root", lambda p: True)
        monkeypatch.setattr(orchestrator, "_git", lambda *a: " M .loop/state.json\n M src/main.py\n")
        result = orchestrator._dirty_tracked_paths()
        assert result == ["src/main.py"]

    def test_returns_empty_when_not_git_repo(self, monkeypatch) -> None:
        monkeypatch.setattr(orchestrator, "_is_git_repo_root", lambda p: False)
        result = orchestrator._dirty_tracked_paths()
        assert result == []


class TestAtomicWriteJson:
    def test_writes_json(self, tmp_path) -> None:
        target = tmp_path / "out.json"
        orchestrator._atomic_write_json(target, {"key": "value"})
        assert target.exists()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data == {"key": "value"}

    def test_no_tmp_left_on_success(self, tmp_path) -> None:
        target = tmp_path / "out.json"
        orchestrator._atomic_write_json(target, {"a": 1})
        assert not target.with_suffix(".tmp").exists()

    def test_no_tmp_left_on_failure(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "out.json"
        # Pre-create the tmp file to verify it gets cleaned up
        tmp = target.with_suffix(".tmp")
        tmp.write_text("garbage", encoding="utf-8")
        # Make write_text on the tmp path raise
        original_write_text = orchestrator.Path.write_text

        def _failing_write_text(self_path, *args, **kwargs):
            if self_path.suffix == ".tmp":
                raise OSError("simulated write failure")
            return original_write_text(self_path, *args, **kwargs)

        monkeypatch.setattr(orchestrator.Path, "write_text", _failing_write_text)
        with pytest.raises(OSError, match="simulated"):
            orchestrator._atomic_write_json(target, {"a": 1})
        assert not tmp.exists()

    def test_replaces_existing_file(self, tmp_path) -> None:
        target = tmp_path / "out.json"
        target.write_text('{"old": true}', encoding="utf-8")
        orchestrator._atomic_write_json(target, {"new": True})
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data == {"new": True}


class TestCmdExtractDiffValidation:
    def test_rejects_invalid_base(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_is_valid_ref", lambda r: False)
        with pytest.raises(SystemExit) as exc:
            orchestrator.cmd_extract_diff("nonexistent", "HEAD")
        assert exc.value.code == 1
        assert "invalid git ref" in capsys.readouterr().err

    def test_rejects_invalid_head(self, monkeypatch, capsys) -> None:
        calls: list[str] = []

        def fake_valid_ref(r):
            calls.append(r)
            return r == "HEAD"

        monkeypatch.setattr(orchestrator, "_is_valid_ref", fake_valid_ref)
        with pytest.raises(SystemExit) as exc:
            orchestrator.cmd_extract_diff("HEAD", "nonexistent")
        assert exc.value.code == 1

    def test_passes_valid_refs(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_is_valid_ref", lambda r: True)
        monkeypatch.setattr(orchestrator, "_diff", lambda b, h: f"diff {b}..{h}")
        orchestrator.cmd_extract_diff("HEAD~1", "HEAD")
        assert "diff HEAD~1..HEAD" in capsys.readouterr().out


class TestCmdArchiveRestoreTraversal:
    def test_rejects_path_traversal(self, monkeypatch, capsys, tmp_path) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        archive_dir = tmp_path / ".loop" / "archive" / "T-001"
        archive_dir.mkdir(parents=True)
        (archive_dir / "r1_state.json").write_text("{}")

        with pytest.raises(SystemExit) as exc:
            orchestrator.cmd_archive("T-001", restore="../../etc/passwd")
        assert exc.value.code == 1
        assert "escapes archive directory" in capsys.readouterr().err

    def test_valid_restore_succeeds(self, monkeypatch, tmp_path) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        archive_dir = tmp_path / ".loop" / "archive" / "T-001"
        archive_dir.mkdir(parents=True)
        (archive_dir / "r1_state.json").write_text('{"round": 1}')

        orchestrator.cmd_archive("T-001", restore="r1_state")
        state_file = tmp_path / ".loop" / "state.json"
        assert state_file.exists()
        assert json.loads(state_file.read_text(encoding="utf-8"))["round"] == 1


class TestStreamDispatchSuppression:
    def test_step_turn_completed_suppressed_by_exact_match(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_stream_local", threading.local())
        parse_event_fn = orchestrator._require_registered_parse_event("codex")

        for line_str in ["[worker] Step completed", "[worker] Turn completed", "[worker] Turn started"]:
            orchestrator._stream_dispatch_stdout_line("worker", "codex", line_str + "\n", parse_event_fn, verbose=False)

        assert capsys.readouterr().out == ""

    def test_agent_message_with_turn_started_not_suppressed(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_stream_local", threading.local())

        line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Turn started on this task"},
            }
        )

        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", line + "\n", orchestrator._codex_event_summary, verbose=False
        )

        out = capsys.readouterr().out
        assert "[worker] Message: Turn started on this task" in out

    def test_agent_message_with_step_completed_not_suppressed(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_stream_local", threading.local())

        line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Step completed successfully"},
            }
        )

        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", line + "\n", orchestrator._codex_event_summary, verbose=False
        )

        out = capsys.readouterr().out
        assert "[worker] Message: Step completed successfully" in out

    def test_dedupe_only_applies_to_tool_use_not_messages(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_stream_local", threading.local())

        msg_line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Working on it"},
            }
        )

        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", msg_line + "\n", orchestrator._codex_event_summary, verbose=False
        )
        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", msg_line + "\n", orchestrator._codex_event_summary, verbose=False
        )

        out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(out_lines) == 2
        assert out_lines[0] == "[worker] Message: Working on it"
        assert out_lines[1] == "[worker] Message: Working on it"

    def test_dedupe_consecutive_identical_running_lines(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_stream_local", threading.local())

        run_line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "git status"},
            }
        )

        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", run_line + "\n", orchestrator._codex_event_summary, verbose=False
        )
        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", run_line + "\n", orchestrator._codex_event_summary, verbose=False
        )

        out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(out_lines) == 1
        assert out_lines[0] == "[worker] Running: git status"

    def test_dedupe_resets_after_different_summary(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_stream_local", threading.local())

        run_line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "git status"},
            }
        )
        msg_line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "hello"},
            }
        )

        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", run_line + "\n", orchestrator._codex_event_summary, verbose=False
        )
        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", msg_line + "\n", orchestrator._codex_event_summary, verbose=False
        )
        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", run_line + "\n", orchestrator._codex_event_summary, verbose=False
        )

        out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(out_lines) == 3
        assert "[worker] Running: git status" in out_lines[0]
        assert "[worker] Message: hello" in out_lines[1]
        assert "[worker] Running: git status" in out_lines[2]

    def test_session_id_deduplicated_across_roles(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(orchestrator, "_stream_local", threading.local())

        line_1 = json.dumps({"type": "thread.started", "thread_id": "tid-999"})
        line_2 = json.dumps({"type": "thread.started", "thread_id": "tid-999"})

        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", line_1 + "\n", orchestrator._codex_event_summary, verbose=False
        )
        orchestrator._stream_dispatch_stdout_line(
            "worker", "codex", line_2 + "\n", orchestrator._codex_event_summary, verbose=False
        )

        out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(out_lines) == 1
        assert "Session: tid-999" in out_lines[0]


class TestPathTraversalRejection:
    def test_archive_rejects_dotdot_task_id(self, monkeypatch, capsys, tmp_path) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        with pytest.raises(SystemExit) as exc:
            orchestrator.cmd_archive("../T-001")
        assert exc.value.code == 1
        assert "path traversal" in capsys.readouterr().err

    def test_archive_rejects_slash_task_id(self, monkeypatch, capsys, tmp_path) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        with pytest.raises(SystemExit) as exc:
            orchestrator.cmd_archive("T-001/sub")
        assert exc.value.code == 1
        assert "path traversal" in capsys.readouterr().err

    def test_archive_rejects_backslash_task_id(self, monkeypatch, capsys, tmp_path) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        with pytest.raises(SystemExit) as exc:
            orchestrator.cmd_archive("T-001\\sub")
        assert exc.value.code == 1
        assert "path traversal" in capsys.readouterr().err


class TestFlattenDepthLimit:
    def test_flatten_respects_max_depth(self) -> None:
        deep = {"a": {"b": {"c": {"d": {"e": "deep_value"}}}}}
        result = orchestrator._flatten_text_payload(deep, max_depth=3)
        assert "deep_value" not in result

    def test_flatten_depth_default_allows_shallow(self) -> None:
        shallow = {"text": "hello world"}
        result = orchestrator._flatten_text_payload(shallow)
        assert result == "hello world"

    def test_flatten_depth_zero_returns_empty(self) -> None:
        result = orchestrator._flatten_text_payload({"text": "hello"}, max_depth=0)
        assert result == ""


class TestConfigureLoopPathsGlobals:
    def test_configure_updates_summary_config_tasks_globals(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
        monkeypatch.setattr(orchestrator, "_LOGS_DIR_ENSURED", False)
        monkeypatch.setattr(orchestrator, "_LOGS_DIR_ENSURED_PATH", None)
        orchestrator._set_feed_task_id(None)

        orchestrator._configure_loop_paths(".loop-test")

        assert orchestrator._SUMMARY_FILE == tmp_path / ".loop-test" / "summary.json"
        assert orchestrator._CONFIG_FILE == tmp_path / ".loop-test" / "config.json"
        assert orchestrator._TASKS_DIR == tmp_path / ".loop-test" / "tasks"

        monkeypatch.setattr(orchestrator, "ROOT", Path.cwd())
        orchestrator._configure_loop_paths(".loop")


class TestAutoDispatchConfig:
    def test_auto_dispatch_from_config_when_cli_flag_omitted(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        config_path = tmp_path / ".loop" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text('{"auto_dispatch": true}', encoding="utf-8")

        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "run", "--loop-dir", ".loop"],
        )

        captured: dict[str, bool] = {}

        def fake_cmd_run(
            config: orchestrator.RunConfig,
            single_round: bool,
            round_num: int | None,
            resume: bool = False,
            reset: bool = False,
        ) -> None:
            _ = (single_round, round_num, resume, reset)
            captured["auto_dispatch"] = config.auto_dispatch

        monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)

        orchestrator.main()

        assert captured["auto_dispatch"] is True

    def test_auto_dispatch_cli_flag_overrides_config(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        config_path = tmp_path / ".loop" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text('{"auto_dispatch": false}', encoding="utf-8")

        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "run", "--loop-dir", ".loop", "--auto-dispatch"],
        )

        captured: dict[str, bool] = {}

        def fake_cmd_run(
            config: orchestrator.RunConfig,
            single_round: bool,
            round_num: int | None,
            resume: bool = False,
            reset: bool = False,
        ) -> None:
            _ = (single_round, round_num, resume, reset)
            captured["auto_dispatch"] = config.auto_dispatch

        monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)

        orchestrator.main()

        assert captured["auto_dispatch"] is True

    def test_auto_dispatch_builtin_default_when_no_cli_no_config(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)

        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "run", "--loop-dir", ".loop"],
        )

        captured: dict[str, bool] = {}

        def fake_cmd_run(
            config: orchestrator.RunConfig,
            single_round: bool,
            round_num: int | None,
            resume: bool = False,
            reset: bool = False,
        ) -> None:
            _ = (single_round, round_num, resume, reset)
            captured["auto_dispatch"] = config.auto_dispatch

        monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)

        orchestrator.main()

        assert captured["auto_dispatch"] is False


class TestConfigLoadingPrecedence:
    def test_load_config_prefers_yaml_over_json(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        config_json = tmp_path / ".loop" / "config.json"
        config_yaml = tmp_path / ".loop" / "config.yaml"
        config_json.write_text('{"max_rounds": 2, "dispatch_timeout": 9}', encoding="utf-8")
        config_yaml.write_text("max_rounds: 7", encoding="utf-8")
        monkeypatch.setattr(
            orchestrator,
            "_load_config_from_yaml",
            lambda path: {"max_rounds": 7, "dispatch_timeout": 19} if path == config_yaml else {},
        )

        loaded = orchestrator._load_config()

        assert loaded["max_rounds"] == 7
        assert loaded["dispatch_timeout"] == 19

    def test_load_config_falls_back_to_json_when_yaml_returns_empty(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        config_json = tmp_path / ".loop" / "config.json"
        config_yaml = tmp_path / ".loop" / "config.yaml"
        config_json.write_text('{"max_rounds": 5, "dispatch_timeout": 21}', encoding="utf-8")
        config_yaml.write_text("max_rounds:", encoding="utf-8")
        monkeypatch.setattr(orchestrator, "_load_config_from_yaml", lambda path: {})

        loaded = orchestrator._load_config()

        assert loaded["max_rounds"] == 5
        assert loaded["dispatch_timeout"] == 21

    def test_main_run_env_overrides_file_values(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        config_path = tmp_path / ".loop" / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "max_rounds": 2,
                    "dispatch_timeout": 10,
                    "backend_preference": ["codex"],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("LOOP_MAX_ROUNDS", "8")
        monkeypatch.setenv("LOOP_DISPATCH_TIMEOUT", "27")
        monkeypatch.setenv("LOOP_BACKEND_PREFERENCE", "claude, opencode")

        captured: dict[str, object] = {}

        def fake_cmd_run(
            config: orchestrator.RunConfig,
            single_round: bool,
            round_num: int | None,
            resume: bool = False,
            reset: bool = False,
        ) -> None:
            _ = (single_round, round_num, resume, reset)
            captured["max_rounds"] = config.max_rounds
            captured["dispatch_timeout"] = config.dispatch_timeout
            captured["backend_preference"] = config.backend_preference

        monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
        monkeypatch.setattr(sys, "argv", ["orchestrator.py", "run", "--loop-dir", ".loop"])

        orchestrator.main()

        assert captured["max_rounds"] == 8
        assert captured["dispatch_timeout"] == 27
        assert captured["backend_preference"] == ["claude", "opencode"]

    def test_main_run_cli_overrides_env_values(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        config_path = tmp_path / ".loop" / "config.json"
        config_path.write_text('{"max_rounds": 2, "dispatch_timeout": 10}', encoding="utf-8")
        monkeypatch.setenv("LOOP_MAX_ROUNDS", "8")
        monkeypatch.setenv("LOOP_DISPATCH_TIMEOUT", "27")
        monkeypatch.setenv("LOOP_BACKEND_PREFERENCE", "claude, opencode")

        captured: dict[str, object] = {}

        def fake_cmd_run(
            config: orchestrator.RunConfig,
            single_round: bool,
            round_num: int | None,
            resume: bool = False,
            reset: bool = False,
        ) -> None:
            _ = (single_round, round_num, resume, reset)
            captured["max_rounds"] = config.max_rounds
            captured["dispatch_timeout"] = config.dispatch_timeout
            captured["backend_preference"] = config.backend_preference

        monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "orchestrator.py",
                "run",
                "--loop-dir",
                ".loop",
                "--max-rounds",
                "11",
                "--dispatch-timeout",
                "31",
            ],
        )

        orchestrator.main()

        assert captured["max_rounds"] == 11
        assert captured["dispatch_timeout"] == 31
        assert captured["backend_preference"] == ["claude", "opencode"]


class TestResetDefault:
    def test_task_card_in_resettable_files(self) -> None:
        assert orchestrator.TASK_CARD in orchestrator._RESETTABLE_FILES

    def test_reset_default_off_no_reset_flag(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.TASK_CARD.write_text('{"task_id":"T-001"}', encoding="utf-8")

        captured: dict[str, bool] = {}

        def fake_cmd_run(
            config: orchestrator.RunConfig,
            single_round: bool,
            round_num: int | None,
            resume: bool = False,
            reset: bool = False,
        ) -> None:
            _ = (config, single_round, round_num, resume)
            captured["reset"] = reset

        monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "run", "--task", str(orchestrator.TASK_CARD)],
        )

        orchestrator.main()

        assert captured["reset"] is False

    def test_reset_opt_in_with_flag(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.TASK_CARD.write_text('{"task_id":"T-001"}', encoding="utf-8")

        captured: dict[str, bool] = {}

        def fake_cmd_run(
            config: orchestrator.RunConfig,
            single_round: bool,
            round_num: int | None,
            resume: bool = False,
            reset: bool = False,
        ) -> None:
            _ = (config, single_round, round_num, resume)
            captured["reset"] = reset

        monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
        monkeypatch.setattr(
            sys,
            "argv",
            ["orchestrator.py", "run", "--task", str(orchestrator.TASK_CARD), "--reset"],
        )

        orchestrator.main()

        assert captured["reset"] is True


class TestRotateLogFile:
    def test_no_rotation_when_under_max_size(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text("small content", encoding="utf-8")
        orchestrator._rotate_log_file(log, max_bytes=1024, backup_count=3)
        assert log.read_text(encoding="utf-8") == "small content"
        assert not (tmp_path / "test.log.1").exists()

    def test_rotates_when_exceeds_max_size(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        big_content = "x" * 100
        log.write_text(big_content, encoding="utf-8")
        orchestrator._rotate_log_file(log, max_bytes=50, backup_count=3)
        assert not log.exists()
        backup = tmp_path / "test.log.1"
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == big_content

    def test_rotates_multiple_backups(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text("content1", encoding="utf-8")
        orchestrator._rotate_log_file(log, max_bytes=1, backup_count=3)
        assert (tmp_path / "test.log.1").read_text(encoding="utf-8") == "content1"

        log.write_text("content2", encoding="utf-8")
        orchestrator._rotate_log_file(log, max_bytes=1, backup_count=3)
        assert (tmp_path / "test.log.1").read_text(encoding="utf-8") == "content2"
        assert (tmp_path / "test.log.2").read_text(encoding="utf-8") == "content1"

        log.write_text("content3", encoding="utf-8")
        orchestrator._rotate_log_file(log, max_bytes=1, backup_count=3)
        assert (tmp_path / "test.log.1").read_text(encoding="utf-8") == "content3"
        assert (tmp_path / "test.log.2").read_text(encoding="utf-8") == "content2"
        assert (tmp_path / "test.log.3").read_text(encoding="utf-8") == "content1"

    def test_drops_oldest_when_exceeds_backup_count(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text("c1", encoding="utf-8")
        orchestrator._rotate_log_file(log, max_bytes=1, backup_count=2)

        log.write_text("c2", encoding="utf-8")
        orchestrator._rotate_log_file(log, max_bytes=1, backup_count=2)

        log.write_text("c3", encoding="utf-8")
        orchestrator._rotate_log_file(log, max_bytes=1, backup_count=2)

        assert (tmp_path / "test.log.1").read_text(encoding="utf-8") == "c3"
        assert (tmp_path / "test.log.2").read_text(encoding="utf-8") == "c2"
        assert not (tmp_path / "test.log.3").exists()

    def test_noop_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.log"
        orchestrator._rotate_log_file(missing, max_bytes=1, backup_count=3)

    def test_default_max_bytes_is_5mb(self) -> None:
        assert orchestrator.DEFAULT_LOG_MAX_BYTES == 5 * 1024 * 1024

    def test_default_backup_count_is_3(self) -> None:
        assert orchestrator.DEFAULT_LOG_BACKUP_COUNT == 3


class TestCmdStatusSummary:
    def test_shows_human_readable_summary_not_raw_json(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.STATE_FILE.write_text(
            json.dumps(
                {
                    "state": "done",
                    "round": 2,
                    "task_id": "T-609",
                    "outcome": "approved",
                    "head_sha": "abc123",
                    "base_sha": "def456",
                    "round_details": [{"round": 1}],
                    "error": "some error",
                    "started_at": "2024-01-01T00:00:00Z",
                    "failed_at": "2024-01-01T01:00:00Z",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        orchestrator.TASK_CARD.write_text("{}", encoding="utf-8")

        orchestrator.cmd_status()
        out = capsys.readouterr().out

        assert "State: done" in out
        assert "Round: 2" in out
        assert "Task ID: T-609" in out
        assert "Outcome: approved" in out
        assert "task_card.json: EXISTS" in out
        assert "work_report.json: missing" in out
        assert "abc123" not in out
        assert "def456" not in out
        assert "round_details" not in out
        assert "some error" not in out
        assert "started_at" not in out
        assert "failed_at" not in out
        assert '"state"' not in out

    def test_shows_state_without_optional_fields(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.STATE_FILE.write_text(
            json.dumps({"state": "idle", "round": 0}),
            encoding="utf-8",
        )

        orchestrator.cmd_status()
        out = capsys.readouterr().out

        assert "State: idle" in out
        assert "Round: 0" in out
        assert "Task ID:" not in out
        assert "Outcome:" not in out


class TestBuildTaskPacket:
    def test_target_files_from_in_scope_glob(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        src = tmp_path / "src" / "loop_kit"
        src.mkdir(parents=True)
        (src / "orchestrator.py").write_text("def foo(): pass\n", encoding="utf-8")
        (src / "utils.py").write_text("def bar(): pass\n", encoding="utf-8")

        task_card = {
            "goal": "test",
            "in_scope": ["src/loop_kit/*.py"],
            "acceptance_criteria": ["must pass tests"],
            "constraints": ["no new dependencies"],
        }

        packet = orchestrator._build_task_packet(task_card, 1)

        assert "src/loop_kit/orchestrator.py" in packet["target_files"]
        assert "src/loop_kit/utils.py" in packet["target_files"]

    def test_target_files_from_in_scope_exact_path(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        src = tmp_path / "src"
        src.mkdir(parents=True)
        (src / "main.py").write_text("def main(): pass\n", encoding="utf-8")

        task_card = {
            "goal": "test",
            "in_scope": ["src/main.py"],
            "acceptance_criteria": [],
            "constraints": [],
        }

        packet = orchestrator._build_task_packet(task_card, 1)

        assert "src/main.py" in packet["target_files"]

    def test_target_symbols_from_function_index(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        src = tmp_path / "src"
        src.mkdir(parents=True)
        (src / "mod.py").write_text("def hello():\n    pass\n\nclass Greeter:\n    pass\n", encoding="utf-8")

        task_card = {
            "goal": "test",
            "in_scope": ["src/mod.py"],
            "acceptance_criteria": [],
            "constraints": [],
        }

        packet = orchestrator._build_task_packet(task_card, 1)

        assert any("def hello" in s for s in packet["target_symbols"])
        assert any("class Greeter" in s for s in packet["target_symbols"])

    def test_invariants_from_constraints(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)

        task_card = {
            "goal": "test",
            "in_scope": [],
            "acceptance_criteria": [],
            "constraints": ["No new dependencies", "Python 3.11+"],
        }

        packet = orchestrator._build_task_packet(task_card, 1)

        assert packet["invariants"] == ["No new dependencies", "Python 3.11+"]

    def test_acceptance_checks_from_acceptance_criteria(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)

        task_card = {
            "goal": "test",
            "in_scope": [],
            "acceptance_criteria": ["All tests pass", "No lint errors"],
            "constraints": [],
        }

        packet = orchestrator._build_task_packet(task_card, 1)

        assert packet["acceptance_checks"] == ["All tests pass", "No lint errors"]

    def test_known_risks_from_pitfalls_md(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        context_dir = tmp_path / ".loop" / "context"
        context_dir.mkdir()
        (context_dir / "pitfalls.md").write_text(
            "Risk 1: circular imports\nRisk 2: race conditions\n", encoding="utf-8"
        )

        task_card = {
            "goal": "test",
            "in_scope": [],
            "acceptance_criteria": [],
            "constraints": [],
        }

        packet = orchestrator._build_task_packet(task_card, 1)

        assert "Risk 1: circular imports" in packet["known_risks"]
        assert "Risk 2: race conditions" in packet["known_risks"]

    def test_known_risks_empty_without_pitfalls(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)

        task_card = {
            "goal": "test",
            "in_scope": [],
            "acceptance_criteria": [],
            "constraints": [],
        }

        packet = orchestrator._build_task_packet(task_card, 1)

        assert packet["known_risks"] == []

    def test_commands_to_run_fixed_whitelist(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)

        task_card = {
            "goal": "test",
            "in_scope": [],
            "acceptance_criteria": [],
            "constraints": [],
        }

        packet = orchestrator._build_task_packet(task_card, 1)

        assert packet["commands_to_run"] == [
            "uv run --group dev pytest",
            "uv run python -m py_compile src/loop_kit/orchestrator.py",
        ]

    def test_round2_includes_fix_list_as_known_risks(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.FIX_LIST.write_text(
            json.dumps(
                {
                    "task_id": "T-611",
                    "round": 2,
                    "fixes": [
                        {"severity": "high", "file": "src/orchestrator.py", "reason": "fix type error"},
                        {"severity": "medium", "file": "tests/test_orchestrator.py", "reason": "add test"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        task_card = {
            "goal": "test",
            "in_scope": [],
            "acceptance_criteria": [],
            "constraints": [],
        }

        packet = orchestrator._build_task_packet(task_card, 2)

        assert "[high] src/orchestrator.py: fix type error" in packet["known_risks"]
        assert "[medium] tests/test_orchestrator.py: add test" in packet["known_risks"]

    def test_graceful_degradation_no_loop_context(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)

        task_card = {
            "goal": "test",
            "in_scope": ["nonexistent/file.py"],
            "acceptance_criteria": [],
            "constraints": [],
        }

        packet = orchestrator._build_task_packet(task_card, 1)

        assert packet["target_files"] == []
        assert packet["target_symbols"] == []
        assert packet["known_risks"] == []

    def test_task_packet_written_before_dispatch(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)

        task_path = tmp_path / "task_input.json"
        task_path.write_text(
            json.dumps(
                {
                    "task_id": "T-611",
                    "goal": "packet test",
                    "in_scope": [],
                    "out_of_scope": [],
                    "acceptance_criteria": ["criteria"],
                    "constraints": ["constraint"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        orchestrator.STATE_FILE.write_text(
            json.dumps(
                {
                    "state": orchestrator.STATE_AWAITING_WORK,
                    "round": 1,
                    "task_id": "T-611",
                    "base_sha": "base-sha",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        def fake_wait(path: Path, description: str, **kwargs) -> dict | None:
            _ = (description, kwargs)
            if path == orchestrator.WORK_REPORT:
                return {
                    "task_id": "T-611",
                    "round": 1,
                    "head_sha": "head-sha",
                    "files_changed": ["src/a.py"],
                    "tests": [],
                    "notes": "done",
                }
            if path == orchestrator.REVIEW_REPORT:
                return {
                    "task_id": "T-611",
                    "round": 1,
                    "decision": "approve",
                    "blocking_issues": [],
                    "non_blocking_suggestions": [],
                }
            return None

        monkeypatch.setattr(orchestrator, "_wait_for_file", fake_wait)
        monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}->{head}")
        monkeypatch.setattr(orchestrator, "_log_oneline", lambda base, head: f"log {base}->{head}")

        orchestrator.cmd_run(
            _run_config(str(task_path)),
            single_round=True,
            round_num=1,
        )

        assert orchestrator.TASK_PACKET.exists()
        data = json.loads(orchestrator.TASK_PACKET.read_text(encoding="utf-8"))
        assert "target_files" in data
        assert "target_symbols" in data
        assert "invariants" in data
        assert data["invariants"] == ["constraint"]
        assert "acceptance_checks" in data
        assert data["acceptance_checks"] == ["criteria"]
        assert "known_risks" in data
        assert "commands_to_run" in data

    def test_render_task_packet_section(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.TASK_PACKET.write_text(
            json.dumps(
                {
                    "target_files": ["src/a.py"],
                    "target_symbols": ["- L1: def foo()"],
                    "invariants": ["no deps"],
                    "acceptance_checks": ["tests pass"],
                    "known_risks": ["risk 1"],
                    "commands_to_run": ["cmd1", "cmd2"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        section = orchestrator._render_task_packet_section()

        assert "target_files:" in section
        assert "src/a.py" in section
        assert "target_symbols:" in section
        assert "def foo" in section
        assert "invariants:" in section
        assert "no deps" in section
        assert "acceptance_checks:" in section
        assert "tests pass" in section
        assert "known_risks:" in section
        assert "risk 1" in section
        assert "commands_to_run:" in section

    def test_render_task_packet_section_empty(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)

        section = orchestrator._render_task_packet_section()

        assert section == "- <none>"

    def test_worker_prompt_round1_includes_task_packet(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.TASK_PACKET.write_text(
            json.dumps(
                {
                    "target_files": ["src/a.py"],
                    "target_symbols": [],
                    "invariants": [],
                    "acceptance_checks": [],
                    "known_risks": [],
                    "commands_to_run": ["cmd"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        def fake_read(path: Path) -> str | None:
            if path.name == "AGENTS.md":
                return "AGENTS_CONTENT"
            if path.name == "code-writer.md":
                return "CODE_WRITER_CONTENT"
            if path == orchestrator._worker_prompt_template_path():
                return None
            return None

        def fake_read_json(path: Path) -> dict | None:
            if path == orchestrator.TASK_CARD:
                return {
                    "goal": "test",
                    "in_scope": [],
                    "out_of_scope": [],
                    "acceptance_criteria": [],
                    "constraints": [],
                }
            if path == orchestrator.TASK_PACKET:
                return {
                    "target_files": ["src/a.py"],
                    "target_symbols": [],
                    "invariants": [],
                    "acceptance_checks": [],
                    "known_risks": [],
                    "commands_to_run": ["cmd"],
                }
            return None

        monkeypatch.setattr(orchestrator, "_read_text_optional", fake_read)
        monkeypatch.setattr(orchestrator, "_read_json_if_exists", fake_read_json)

        prompt = orchestrator._worker_prompt("T-611", 1)

        assert "=== TASK PACKET ===" in prompt
        assert "src/a.py" in prompt

    def test_worker_prompt_round2_includes_task_packet(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.TASK_PACKET.write_text(
            json.dumps(
                {
                    "target_files": ["src/a.py"],
                    "target_symbols": [],
                    "invariants": [],
                    "acceptance_checks": [],
                    "known_risks": ["[high] file.py: fix it"],
                    "commands_to_run": ["cmd"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        def fake_read(path: Path) -> str | None:
            if path.name == "code-writer.md":
                return "CODE_WRITER_CONTENT"
            return None

        def fake_read_json(path: Path) -> dict | None:
            if path == orchestrator.TASK_CARD:
                return {
                    "goal": "test",
                    "in_scope": [],
                    "out_of_scope": [],
                    "acceptance_criteria": [],
                    "constraints": [],
                }
            if path == orchestrator.TASK_PACKET:
                return {
                    "target_files": ["src/a.py"],
                    "target_symbols": [],
                    "invariants": [],
                    "acceptance_checks": [],
                    "known_risks": ["[high] file.py: fix it"],
                    "commands_to_run": ["cmd"],
                }
            if path == orchestrator.WORK_REPORT:
                return {"notes": "prev", "files_changed": ["file.py"]}
            if path == orchestrator.REVIEW_REPORT:
                return {
                    "blocking_issues": [{"severity": "high", "file": "file.py", "reason": "fix it"}],
                    "non_blocking_suggestions": [],
                }
            if path == orchestrator.FIX_LIST:
                return {
                    "task_id": "T-611",
                    "round": 2,
                    "fixes": [{"severity": "high", "file": "file.py", "reason": "fix it"}],
                }
            return None

        monkeypatch.setattr(orchestrator, "_read_text_optional", fake_read)
        monkeypatch.setattr(orchestrator, "_read_json_if_exists", fake_read_json)

        prompt = orchestrator._worker_prompt("T-611", 2)

        assert "=== TASK PACKET ===" in prompt
        assert "src/a.py" in prompt


class TestKnowledgeLayer:
    def test_worker_prompt_round1_injects_knowledge_section(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        context_dir = tmp_path / ".loop" / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "project_facts.md").write_text(
            "# facts\n- single-file rule\n- subprocess-per-round\n",
            encoding="utf-8",
        )
        (context_dir / "pitfalls.md").write_text("# pitfalls\n- stale lock handling\n", encoding="utf-8")
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale_iso = (datetime.now(UTC) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (context_dir / "patterns.jsonl").write_text(
            json.dumps(
                {
                    "pattern": "fresh pattern",
                    "category": "workflow",
                    "confidence": 0.9,
                    "last_verified": now_iso,
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "pattern": "low confidence pattern",
                    "category": "workflow",
                    "confidence": 0.2,
                    "last_verified": now_iso,
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "pattern": "stale pattern",
                    "category": "workflow",
                    "confidence": 0.9,
                    "last_verified": stale_iso,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        prompt = orchestrator._worker_prompt("T-612", 1)

        assert "=== KNOWLEDGE ===" in prompt
        assert "single-file rule" in prompt
        assert "stale lock handling" in prompt
        assert "fresh pattern" in prompt
        assert "low confidence pattern" not in prompt
        assert "stale pattern" not in prompt

    def test_worker_prompt_graceful_degradation_when_context_missing(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        prompt = orchestrator._worker_prompt("T-612", 1)
        assert "=== KNOWLEDGE ===" in prompt
        assert "=== KNOWLEDGE ===\n- <none>" in prompt

    def test_patterns_staleness_governance_sets_confidence_zero(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        context_dir = tmp_path / ".loop" / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        stale_iso = (datetime.now(UTC) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (context_dir / "patterns.jsonl").write_text(
            json.dumps(
                {
                    "pattern": "stale pattern",
                    "category": "quality",
                    "confidence": 0.9,
                    "last_verified": stale_iso,
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "pattern": "fresh pattern",
                    "category": "quality",
                    "confidence": 0.9,
                    "last_verified": fresh_iso,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        entries, stale_count = orchestrator._load_patterns_with_governance(persist=True)

        assert stale_count == 1
        by_pattern = {item["pattern"]: item for item in entries}
        assert by_pattern["stale pattern"]["confidence"] == 0.0
        assert by_pattern["fresh pattern"]["confidence"] == 0.9

        persisted = [
            json.loads(line)
            for line in (context_dir / "patterns.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        persisted_by_pattern = {item["pattern"]: item for item in persisted}
        assert persisted_by_pattern["stale pattern"]["confidence"] == 0.0

    def test_patterns_deduplicated_by_category_and_pattern_keep_highest_confidence(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        context_dir = tmp_path / ".loop" / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (context_dir / "patterns.jsonl").write_text(
            json.dumps(
                {
                    "pattern": "run tests",
                    "category": "workflow",
                    "confidence": 0.2,
                    "last_verified": now_iso,
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "pattern": "run tests",
                    "category": "workflow",
                    "confidence": 0.9,
                    "last_verified": now_iso,
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "pattern": "run tests",
                    "category": "quality",
                    "confidence": 0.4,
                    "last_verified": now_iso,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        feed_events: list[tuple[str, str, dict | None]] = []

        def fake_feed_event(event: str, *, level: str = "info", data: dict | None = None, **kwargs) -> None:
            _ = kwargs
            feed_events.append((event, level, data))

        monkeypatch.setattr(orchestrator, "_feed_event", fake_feed_event)

        entries, stale_count = orchestrator._load_patterns_with_governance(persist=False)

        assert stale_count == 0
        assert len(entries) == 2
        by_key = {(item["category"], item["pattern"]): item for item in entries}
        assert by_key[("workflow", "run tests")]["confidence"] == 0.9
        assert by_key[("quality", "run tests")]["confidence"] == 0.4
        dedupe_logs = [
            data
            for event, level, data in feed_events
            if event == orchestrator.FEED_LOG and level == "debug" and isinstance(data, dict)
        ]
        assert len(dedupe_logs) == 1
        assert dedupe_logs[0]["message"] == "Pattern deduplication: 1 duplicates removed, 2 unique kept"

    def test_source_version_is_populated_for_loaded_knowledge(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        context_dir = tmp_path / ".loop" / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "project_facts.md").write_text(
            "# facts\n- single-file rule\n- subprocess-per-round\n",
            encoding="utf-8",
        )
        (context_dir / "pitfalls.md").write_text("# pitfalls\n- stale lock handling\n", encoding="utf-8")
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (context_dir / "patterns.jsonl").write_text(
            json.dumps(
                {
                    "pattern": "fresh pattern",
                    "category": "workflow",
                    "confidence": 0.9,
                    "last_verified": now_iso,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        facts = orchestrator._load_project_facts()
        pitfalls = orchestrator._load_pitfalls()
        patterns, _ = orchestrator._load_patterns_with_governance(persist=False)

        expected_facts_version = hashlib.sha1((context_dir / "project_facts.md").read_bytes()).hexdigest()[:8]
        expected_pitfalls_version = hashlib.sha1((context_dir / "pitfalls.md").read_bytes()).hexdigest()[:8]
        expected_patterns_version = hashlib.sha1((context_dir / "patterns.jsonl").read_bytes()).hexdigest()[:8]
        assert facts and pitfalls and patterns
        assert all(item["source_version"] == expected_facts_version for item in facts)
        assert all(item["source_version"] == expected_pitfalls_version for item in pitfalls)
        assert all(item["source_version"] == expected_patterns_version for item in patterns)
        assert all(len(item["source_version"]) == 8 for item in facts + pitfalls + patterns)

    def test_append_pitfalls_enforces_max_lines_and_preserves_header_lines(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(orchestrator, "_KNOWLEDGE_MAX_PITFALL_LINES", 3)
        context_dir = tmp_path / ".loop" / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "pitfalls.md").write_text(
            "# Known pitfalls\n\n- old one\n- old two\n- old three\n",
            encoding="utf-8",
        )

        appended = orchestrator._append_pitfalls(["new one", "new two"])

        assert appended == 2
        lines = (context_dir / "pitfalls.md").read_text(encoding="utf-8").splitlines()
        assert lines[:2] == ["# Known pitfalls", ""]
        assert [line for line in lines if line.startswith("- ")] == [
            "- old three",
            "- new one",
            "- new two",
        ]

    def test_update_knowledge_on_approval_prunes_patterns_fifo(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(orchestrator, "_KNOWLEDGE_MAX_PATTERNS", 3)
        context_dir = tmp_path / ".loop" / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (context_dir / "patterns.jsonl").write_text(
            json.dumps(
                {
                    "pattern": "old-1",
                    "category": "workflow",
                    "confidence": 0.9,
                    "last_verified": now_iso,
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "pattern": "old-2",
                    "category": "workflow",
                    "confidence": 0.9,
                    "last_verified": now_iso,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        orchestrator.REVIEW_REPORT.write_text(
            json.dumps(
                {
                    "task_id": "T-612",
                    "round": 2,
                    "decision": "approve",
                    "blocking_issues": [],
                    "non_blocking_suggestions": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        archive_dir = tmp_path / ".loop" / "archive" / "T-612"
        archive_dir.mkdir(parents=True)
        (archive_dir / "r2_review_report.json").write_text(
            json.dumps(
                {
                    "task_id": "T-612",
                    "round": 1,
                    "decision": "changes_required",
                    "blocking_issues": [
                        {"severity": "high", "file": "a.py", "reason": "new-1"},
                        {"severity": "high", "file": "b.py", "reason": "new-2"},
                        {"severity": "high", "file": "c.py", "reason": "new-3"},
                    ],
                    "non_blocking_suggestions": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        orchestrator._update_knowledge_on_approval("T-612", 2)

        pattern_entries = [
            json.loads(line)
            for line in (context_dir / "patterns.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert [entry["pattern"] for entry in pattern_entries] == ["new-1", "new-2", "new-3"]

    def test_update_knowledge_on_approval_uses_review_blocking_issues_only(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.WORK_REPORT.write_text(
            json.dumps(
                {
                    "task_id": "T-612",
                    "round": 1,
                    "notes": "worker-only-notes must not be used for knowledge updates",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        orchestrator.REVIEW_REPORT.write_text(
            json.dumps(
                {
                    "task_id": "T-612",
                    "round": 2,
                    "decision": "approve",
                    "blocking_issues": [],
                    "non_blocking_suggestions": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        archive_dir = tmp_path / ".loop" / "archive" / "T-612"
        archive_dir.mkdir(parents=True)
        (archive_dir / "r2_review_report.json").write_text(
            json.dumps(
                {
                    "task_id": "T-612",
                    "round": 1,
                    "decision": "changes_required",
                    "blocking_issues": [
                        {"severity": "high", "file": "src/loop_kit/orchestrator.py", "reason": "retry replace on nt"},
                        {"severity": "medium", "file": "tests/test_orchestrator.py", "reason": "cover stale patterns"},
                    ],
                    "non_blocking_suggestions": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        orchestrator._update_knowledge_on_approval("T-612", 2)

        pitfalls_text = (tmp_path / ".loop" / "context" / "pitfalls.md").read_text(encoding="utf-8")
        assert "retry replace on nt" in pitfalls_text
        assert "cover stale patterns" in pitfalls_text
        assert "worker-only-notes" not in pitfalls_text

        pattern_entries = [
            json.loads(line)
            for line in (tmp_path / ".loop" / "context" / "patterns.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        reasons = {entry["pattern"] for entry in pattern_entries}
        assert "retry replace on nt" in reasons
        assert "cover stale patterns" in reasons
        for entry in pattern_entries:
            if entry["pattern"] in reasons:
                assert entry["category"] == "review_blocking_issue"

    def test_single_round_updates_knowledge_only_on_approve(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        task_path = tmp_path / "task_input.json"
        task_path.write_text(
            json.dumps(
                {
                    "task_id": "T-612",
                    "goal": "knowledge update hook",
                    "in_scope": [],
                    "out_of_scope": [],
                    "acceptance_criteria": [],
                    "constraints": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        orchestrator.STATE_FILE.write_text(
            json.dumps(
                {
                    "state": orchestrator.STATE_AWAITING_WORK,
                    "round": 1,
                    "task_id": "T-612",
                    "base_sha": "base-sha",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        def fake_wait(path: Path, description: str, **kwargs) -> dict | None:
            _ = (description, kwargs)
            if path == orchestrator.WORK_REPORT:
                return {
                    "task_id": "T-612",
                    "round": 1,
                    "head_sha": "head-sha",
                    "files_changed": ["src/loop_kit/orchestrator.py"],
                    "tests": [],
                    "notes": "done",
                }
            if path == orchestrator.REVIEW_REPORT:
                return {
                    "task_id": "T-612",
                    "round": 1,
                    "decision": "approve",
                    "blocking_issues": [],
                    "non_blocking_suggestions": [],
                }
            return None

        calls: list[tuple[str, int]] = []

        monkeypatch.setattr(orchestrator, "_wait_for_file", fake_wait)
        monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}->{head}")
        monkeypatch.setattr(orchestrator, "_log_oneline", lambda base, head: f"log {base}->{head}")
        monkeypatch.setattr(
            orchestrator, "_update_knowledge_on_approval", lambda task_id, round_num: calls.append((task_id, round_num))
        )

        orchestrator.cmd_run(
            _run_config(str(task_path)),
            single_round=True,
            round_num=1,
        )

        assert calls == [("T-612", 1)]

    def test_single_round_does_not_update_knowledge_when_not_approved(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        task_path = tmp_path / "task_input.json"
        task_path.write_text(
            json.dumps(
                {
                    "task_id": "T-612",
                    "goal": "knowledge update hook",
                    "in_scope": [],
                    "out_of_scope": [],
                    "acceptance_criteria": [],
                    "constraints": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        orchestrator.STATE_FILE.write_text(
            json.dumps(
                {
                    "state": orchestrator.STATE_AWAITING_WORK,
                    "round": 1,
                    "task_id": "T-612",
                    "base_sha": "base-sha",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        def fake_wait(path: Path, description: str, **kwargs) -> dict | None:
            _ = (description, kwargs)
            if path == orchestrator.WORK_REPORT:
                return {
                    "task_id": "T-612",
                    "round": 1,
                    "head_sha": "head-sha",
                    "files_changed": ["src/loop_kit/orchestrator.py"],
                    "tests": [],
                    "notes": "done",
                }
            if path == orchestrator.REVIEW_REPORT:
                return {
                    "task_id": "T-612",
                    "round": 1,
                    "decision": "changes_required",
                    "blocking_issues": [{"severity": "high", "file": "x.py", "reason": "fix"}],
                    "non_blocking_suggestions": [],
                }
            return None

        calls: list[tuple[str, int]] = []

        monkeypatch.setattr(orchestrator, "_wait_for_file", fake_wait)
        monkeypatch.setattr(orchestrator, "_diff", lambda base, head: f"diff {base}->{head}")
        monkeypatch.setattr(orchestrator, "_log_oneline", lambda base, head: f"log {base}->{head}")
        monkeypatch.setattr(
            orchestrator, "_update_knowledge_on_approval", lambda task_id, round_num: calls.append((task_id, round_num))
        )

        orchestrator.cmd_run(
            _run_config(str(task_path)),
            single_round=True,
            round_num=1,
        )

        assert calls == []


def test_state_migration_from_version_0_to_1(tmp_path: Path, monkeypatch) -> None:
    """Test that old state.json without version field is migrated to version 1."""
    _configure_loop_paths(monkeypatch, tmp_path)

    # Create an old state file (version 0) with only basic fields
    old_state = {
        "state": orchestrator.STATE_AWAITING_WORK,
        "round": 2,
        "task_id": "T-123",
        # No version field
    }
    orchestrator.STATE_FILE.write_text(json.dumps(old_state), encoding="utf-8")

    # Load state should migrate it
    loaded_state = orchestrator._load_state()

    # Verify migration
    assert loaded_state["version"] == orchestrator.STATE_SCHEMA_VERSION
    assert loaded_state["state"] == old_state["state"]
    assert loaded_state["round"] == old_state["round"]
    assert loaded_state["task_id"] == old_state["task_id"]


def test_state_migration_adds_missing_core_fields(tmp_path: Path, monkeypatch) -> None:
    """Test migration adds missing core fields with defaults."""
    _configure_loop_paths(monkeypatch, tmp_path)

    # Old state with version 0 but missing some core fields
    old_state = {
        "state": orchestrator.STATE_DONE,
        # missing round and task_id
    }
    orchestrator.STATE_FILE.write_text(json.dumps(old_state), encoding="utf-8")

    loaded_state = orchestrator._load_state()

    assert loaded_state["version"] == orchestrator.STATE_SCHEMA_VERSION
    assert loaded_state["state"] == orchestrator.STATE_DONE
    assert loaded_state["round"] == 0  # default
    assert loaded_state["task_id"] is None  # default


def test_state_migration_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)

    orchestrator.STATE_FILE.write_text(
        json.dumps({"state": orchestrator.STATE_AWAITING_WORK, "round": 1}, ensure_ascii=False),
        encoding="utf-8",
    )

    first = orchestrator._load_state()
    second = orchestrator._migrate_state_schema(first)

    assert first == second
    assert second["version"] == orchestrator.STATE_SCHEMA_VERSION
