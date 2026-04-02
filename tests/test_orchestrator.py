from __future__ import annotations

import json
import subprocess
import sys
import threading
import builtins
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

    def wait(self) -> int:
        self.wait_called = True
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


def test_agent_command_claude_keeps_prompt_in_cli_and_no_stdin(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_resolve_backend_exe", lambda backend: f"{backend}.exe")
    prompt = "claude prompt payload"

    cmd, session_id, stdin_text = orchestrator._agent_command("claude", prompt)

    assert cmd[0] == "claude.exe"
    assert "--session-id" in cmd
    assert cmd[-1] == prompt
    assert isinstance(session_id, str) and session_id
    assert stdin_text is None


def test_agent_command_unknown_backend_lists_available_backends() -> None:
    with pytest.raises(ValueError) as exc:
        orchestrator._agent_command("unknown-backend", "payload")

    message = str(exc.value)
    assert "Unsupported backend: unknown-backend" in message
    assert "Registered backends:" in message
    assert "claude" in message
    assert "codex" in message


def test_main_loop_dir_overrides_all_bus_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    captured: dict[str, Path] = {}

    def fake_status() -> None:
        captured["loop_dir"] = orchestrator.LOOP_DIR
        captured["logs_dir"] = orchestrator.LOGS_DIR
        captured["runtime_dir"] = orchestrator.RUNTIME_DIR
        captured["archive_dir"] = orchestrator.ARCHIVE_DIR
        captured["state_file"] = orchestrator.STATE_FILE
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
    assert captured["state_file"] == expected_loop / "state.json"
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


def test_register_backend_allows_custom_backend_in_run_cli(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "_BACKEND_REGISTRY", dict(orchestrator._BACKEND_REGISTRY))

    def resolve_exe(backend: str) -> str:
        return f"{backend}.exe"

    def build_cmd(exe: str, prompt: str) -> tuple[list[str], str | None, str | None]:
        return [exe, "--prompt", prompt], "my-session", None

    orchestrator.register_backend("mybackend", build_cmd, resolve_exe)
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
    ) -> None:
        _ = (single_round, round_num, resume)
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


def test_run_auto_dispatch_timeout_kills_and_waits_process(monkeypatch) -> None:
    captured: dict[str, object] = {}

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
    assert "try --dispatch-timeout 900" in str(exc.value)


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


def test_run_auto_dispatch_streams_compact_summaries_in_non_verbose_mode(
    monkeypatch, capsys
) -> None:
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
    assert raw_lines[0].strip() in out
    assert raw_lines[1].strip() in out
    assert "[worker] Message:" not in out


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


def test_stream_dispatch_stdout_line_collapses_consecutive_reads(monkeypatch, capsys) -> None:
    monkeypatch.setattr(orchestrator, "_stream_local", threading.local())
    line_1 = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "Get-Content -Path src/loop_kit/orchestrator.py | Select-Object -Skip 0 -First 40",
        },
    })
    line_2 = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "cat src/loop_kit/orchestrator.py | Select-Object -Skip 40 -First 40",
        },
    })

    orchestrator._stream_dispatch_stdout_line("reader", "codex", line_1 + "\n", verbose=False)
    orchestrator._stream_dispatch_stdout_line("reader", "codex", line_2 + "\n", verbose=False)

    out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert out_lines == ["[reader] Reading: orchestrator.py"]


def test_stream_dispatch_stdout_line_read_collapse_resets_on_non_summary_line(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(orchestrator, "_stream_local", threading.local())
    read_line = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "Get-Content src/loop_kit/orchestrator.py | Select-Object -Skip 0 -First 40",
        },
    })

    orchestrator._stream_dispatch_stdout_line("reader", "codex", read_line + "\n", verbose=False)
    orchestrator._stream_dispatch_stdout_line("reader", "codex", "plain status line\n", verbose=False)
    orchestrator._stream_dispatch_stdout_line("reader", "codex", read_line + "\n", verbose=False)

    out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert out_lines == [
        "[reader] Reading: orchestrator.py",
        "[reader] Reading: orchestrator.py",
    ]


def test_run_auto_dispatch_dispatch_log_keeps_full_stdout_when_non_verbose(
    tmp_path: Path, monkeypatch, capsys
) -> None:
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
    assert len(popen_calls) == 3
    assert sleep_calls == [5, 10]


def test_worker_prompt_round1_includes_task_card_section(monkeypatch) -> None:
    def fake_read(path: Path) -> str | None:
        if path.name == "AGENTS.md":
            return "AGENTS_CONTENT"
        if path.name == "code-writer.md":
            return "CODE_WRITER_CONTENT"
        if path == orchestrator._worker_prompt_template_path():
            return orchestrator.DEFAULT_WORKER_PROMPT_TEMPLATE
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


def test_worker_prompt_round2_includes_prior_round_context(monkeypatch) -> None:
    def fake_read(path: Path) -> str | None:
        if path.name == "AGENTS.md":
            return "AGENTS_CONTENT"
        if path.name == "code-writer.md":
            return "CODE_WRITER_CONTENT"
        if path == orchestrator._worker_prompt_template_path():
            return orchestrator.DEFAULT_WORKER_PROMPT_TEMPLATE
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
                "blocking_issues": [
                    {"severity": "high", "file": "tools/orchestrator.py", "reason": "fix me"}
                ],
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


def test_worker_prompt_loads_template_when_file_exists(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "AGENTS.md").write_text("AGENTS_CONTENT", encoding="utf-8")
    role_path = tmp_path / "docs" / "roles" / "code-writer.md"
    role_path.parent.mkdir(parents=True, exist_ok=True)
    role_path.write_text("CODE_WRITER_CONTENT", encoding="utf-8")
    orchestrator.TASK_CARD.write_text(
        json.dumps(
            {
                "task_id": "T-611",
                "goal": "Use custom template",
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

    assert prompt.startswith("CUSTOM T-611 1")
    assert "AGENTS_CONTENT" in prompt
    assert "CODE_WRITER_CONTENT" in prompt
    assert "goal: Use custom template" in prompt


def test_worker_prompt_raises_when_template_missing(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "AGENTS.md").write_text("AGENTS_CONTENT", encoding="utf-8")
    role_path = tmp_path / "docs" / "roles" / "code-writer.md"
    role_path.parent.mkdir(parents=True, exist_ok=True)
    role_path.write_text("CODE_WRITER_CONTENT", encoding="utf-8")
    orchestrator.TASK_CARD.write_text(
        json.dumps(
            {
                "task_id": "T-611",
                "goal": "Fallback template",
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

    with pytest.raises(RuntimeError) as exc:
        orchestrator._worker_prompt("T-611", 1)

    message = str(exc.value)
    assert "Missing required prompt template" in message
    assert "Run 'loop init' to create it" in message


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


def test_worker_prompt_raises_when_code_writer_role_doc_missing(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "docs" / "roles" / "code-writer.md").unlink()

    with pytest.raises(RuntimeError) as exc:
        orchestrator._worker_prompt("T-613", 1)

    message = str(exc.value)
    assert "Missing required code-writer role doc" in message
    assert "Create this file and re-run" in message


def test_worker_prompt_raises_when_agents_doc_missing(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "AGENTS.md").unlink()

    with pytest.raises(RuntimeError) as exc:
        orchestrator._worker_prompt("T-613", 1)

    message = str(exc.value)
    assert "Missing required AGENTS.md" in message
    assert "Create this file and re-run" in message


def test_reviewer_prompt_raises_when_role_doc_missing(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "docs" / "roles" / "reviewer.md").unlink()

    with pytest.raises(RuntimeError) as exc:
        orchestrator._reviewer_prompt("T-613", 1)

    message = str(exc.value)
    assert "Missing required reviewer role doc" in message
    assert "Create this file and re-run" in message


def test_log_writes_jsonl_feed_entry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "LOGS_DIR", tmp_path)

    orchestrator._log("structured log message")

    feed_path = tmp_path / "feed.jsonl"
    assert feed_path.exists()
    entries = [json.loads(line) for line in feed_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert entries
    latest = entries[-1]
    assert set(latest.keys()) == {"ts", "level", "event", "data"}
    assert latest["level"] == "info"
    assert latest["event"] == "log"
    assert latest["data"]["message"] == "structured log message"


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
        json.loads(line)
        for line in (tmp_path / "feed.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
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
    ) -> None:
        captured["artifact_timeout"] = config.artifact_timeout
        captured["single_round"] = single_round
        captured["round_num"] = round_num
        captured["resume"] = resume
        captured["verbose"] = config.verbose

    monkeypatch.setattr(orchestrator, "cmd_run", fake_cmd_run)
    monkeypatch.setattr(sys, "argv", [
        "orchestrator.py",
        "run",
        "--artifact-timeout",
        "123",
        "--verbose",
        "--single-round",
        "--round",
        "7",
    ])

    orchestrator.main()

    assert captured["artifact_timeout"] == 123
    assert captured["single_round"] is True
    assert captured["round_num"] == 7
    assert captured["resume"] is False
    assert captured["verbose"] is True


def test_main_run_parses_dispatch_retry_flags(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def fake_cmd_run(
        config: orchestrator.RunConfig,
        single_round: bool,
        round_num: int | None,
        resume: bool = False,
    ) -> None:
        _ = (single_round, round_num, resume)
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
    monkeypatch.setattr(orchestrator, "TASK_CARD", loop_dir / "task_card.json")
    monkeypatch.setattr(orchestrator, "FIX_LIST", loop_dir / "fix_list.json")
    monkeypatch.setattr(orchestrator, "WORK_REPORT", loop_dir / "work_report.json")
    monkeypatch.setattr(orchestrator, "REVIEW_REQ", loop_dir / "review_request.json")
    monkeypatch.setattr(orchestrator, "REVIEW_REPORT", loop_dir / "review_report.json")
    monkeypatch.setattr(orchestrator, "LOCK_FILE", loop_dir / "lock")
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


def test_outer_loop_streams_single_round_subprocess_stdout_in_real_time(
    tmp_path: Path, monkeypatch, capsys
) -> None:
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


def test_outer_loop_continues_from_state_without_fresh_review_report(
    tmp_path: Path, monkeypatch
) -> None:
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


def test_cmd_run_resume_done_approved_exits_cleanly(
    tmp_path: Path, monkeypatch, capsys
) -> None:
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
    monkeypatch.setattr(orchestrator, "_run_multi_round_via_subprocess", lambda **kwargs: called.update({"outer": True}))

    orchestrator.cmd_run(
        _run_config(str(task_path)),
        single_round=False,
        round_num=None,
        resume=True,
    )

    assert called["outer"] is False
    out = capsys.readouterr().out
    assert "done/approved" in out


def test_cmd_run_resume_failed_state_prints_error_and_exits_3(
    tmp_path: Path, monkeypatch, capsys
) -> None:
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
        from datetime import datetime, timezone
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
        stdout = (
            '{"type":"thread.started","thread_id":"tid_123"}\n'
            '{"type":"item.completed","item":{}}\n'
        )
        assert orchestrator._extract_codex_thread_id(stdout) == "tid_123"

    def test_returns_none_on_no_match(self) -> None:
        stdout = '{"type":"item.completed"}\n'
        assert orchestrator._extract_codex_thread_id(stdout) is None

    def test_ignores_malformed_json(self) -> None:
        stdout = "not json\n{}\n"
        assert orchestrator._extract_codex_thread_id(stdout) is None


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
        line = json.dumps({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "npm test"},
        })
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert "[worker] Running: npm test" == result

    def test_agent_message(self) -> None:
        line = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "I fixed the bug."},
        })
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert "[worker] Message:" in result
        assert "I fixed the bug." in result

    def test_file_change(self) -> None:
        line = json.dumps({
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [
                    {"path": "src/loop_kit/orchestrator.py"},
                    {"path": "tests/test_orchestrator.py"},
                ],
            },
        })
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert "[worker] Editing: orchestrator.py, test_orchestrator.py" == result

    def test_top_level_file_change_uses_short_filename(self) -> None:
        line = json.dumps({
            "type": "file_change",
            "changes": [
                {"path": "src/loop_kit/orchestrator.py"},
            ],
        })
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert "[worker] Editing: orchestrator.py" == result

    def test_file_change_keeps_colliding_basenames(self) -> None:
        line = json.dumps({
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [
                    {"path": "src/api/config.py"},
                    {"path": "tests/config.py"},
                ],
            },
        })
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert "[worker] Editing: api/config.py, tests/config.py" == result

    def test_command_execution_strips_powershell_wrapper(self) -> None:
        line = json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": (
                    '"C:\\Program Files\\PowerShell\\7\\pwsh.exe" '
                    "-Command Get-Content -Path src/loop_kit/orchestrator.py "
                    "| Select-Object -Skip 40 -First 20"
                ),
            },
        })
        result = orchestrator._codex_event_summary("worker", "codex", line)
        assert result is not None
        assert (
            "[worker] Running: Get-Content -Path src/loop_kit/orchestrator.py "
            "| Select-Object -Skip 40 -First 20"
        ) == result

    def test_item_started(self) -> None:
        line = json.dumps({
            "type": "item.started",
            "item": {"type": "command_execution"},
        })
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
        result = orchestrator._dispatch_failure_hint(backend="codex", stderr="", timeout=True)
        assert "--dispatch-timeout" in result

    def test_auth_hint_codex(self) -> None:
        result = orchestrator._dispatch_failure_hint(
            backend="codex", stderr="Error: unauthorized 401"
        )
        assert "codex API key" in result

    def test_auth_hint_claude(self) -> None:
        result = orchestrator._dispatch_failure_hint(
            backend="claude", stderr="auth token expired"
        )
        assert "claude authentication" in result

    def test_not_found_hint(self) -> None:
        result = orchestrator._dispatch_failure_hint(
            backend="codex", stderr="codex: command not found"
        )
        assert "executable path" in result

    def test_fallback_hint(self) -> None:
        result = orchestrator._dispatch_failure_hint(backend="codex", stderr="unknown error")
        assert "auth/network" in result


# ── dispatch ─────────────────────────────────────────────────────────


class TestWriteDispatchLog:
    def test_writes_structured_log(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(orchestrator, "LOGS_DIR", tmp_path)
        result = subprocess.CompletedProcess(
            args=["codex"], returncode=0, stdout="out\n", stderr="err\n"
        )
        result.stdout = "out\n"
        result.stderr = "err\n"
        orchestrator._write_dispatch_log(
            "worker", ["codex", "exec"], result, "sid-123"
        )
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
        result = orchestrator._resolve_exe_from_candidates(
            backend="test", candidates=[None, str(exe)]
        )
        assert result == str(exe)

    def test_raises_when_none_found(self) -> None:
        with pytest.raises(RuntimeError, match="Cannot find executable"):
            orchestrator._resolve_exe_from_candidates(
                backend="test", candidates=[None, "/nonexistent/path"]
            )


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
        import os, time
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


class TestValidateWorkReport:
    def test_valid_report(self) -> None:
        work = {"task_id": "T-1", "head_sha": "abc", "round": 1}
        assert orchestrator._validate_work_report(work, expected_task_id="T-1", expected_round=1) is None

    def test_missing_field(self) -> None:
        assert "missing required field" in (
            orchestrator._validate_work_report({}, expected_task_id="T-1", expected_round=1) or ""
        )

    def test_wrong_type_int(self) -> None:
        work = {"task_id": "T-1", "head_sha": "abc", "round": "1"}
        assert "must be int" in (
            orchestrator._validate_work_report(work, expected_task_id="T-1", expected_round=1) or ""
        )

    def test_empty_string(self) -> None:
        work = {"task_id": "  ", "head_sha": "abc", "round": 1}
        assert "non-empty" in (
            orchestrator._validate_work_report(work, expected_task_id="T-1", expected_round=1) or ""
        )

    def test_task_id_mismatch(self) -> None:
        work = {"task_id": "T-2", "head_sha": "abc", "round": 1}
        assert "mismatch" in (
            orchestrator._validate_work_report(work, expected_task_id="T-1", expected_round=1) or ""
        )

    def test_round_mismatch(self) -> None:
        work = {"task_id": "T-1", "head_sha": "abc", "round": 2}
        assert "mismatch" in (
            orchestrator._validate_work_report(work, expected_task_id="T-1", expected_round=1) or ""
        )


class TestLoadState:
    def test_default_when_no_file(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        state = orchestrator._load_state()
        assert state["state"] == "idle"
        assert state["round"] == 0

    def test_loads_existing(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.STATE_FILE.write_text(
            json.dumps({"state": "done", "round": 3}), encoding="utf-8"
        )
        state = orchestrator._load_state()
        assert state["state"] == "done"
        assert state["round"] == 3


class TestSaveState:
    def test_writes_json(self, tmp_path: Path, monkeypatch) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator._save_state({"state": "done", "round": 2})
        data = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
        assert data["state"] == "done"
        assert data["round"] == 2


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
    def test_prints_state_and_file_markers(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.STATE_FILE.write_text(
            json.dumps({"state": "done", "round": 1}), encoding="utf-8"
        )
        orchestrator.TASK_CARD.write_text("{}", encoding="utf-8")
        orchestrator.cmd_status()
        out = capsys.readouterr().out
        assert '"state": "done"' in out
        assert "task_card.json: EXISTS" in out
        assert "work_report.json: missing" in out


class TestCmdHealth:
    def test_reports_both_roles(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _configure_loop_paths(monkeypatch, tmp_path)
        orchestrator.cmd_health(30)
        out = capsys.readouterr().out
        assert "worker:" in out
        assert "reviewer:" in out
        assert "dead" in out  # no heartbeats exist


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
            orchestrator.register_backend("  ", lambda e, p: ([], None, None), lambda b: b)

    def test_strip_and_lower(self, monkeypatch) -> None:
        monkeypatch.setattr(orchestrator, "_resolve_backend_exe", lambda b: "test.exe")
        orchestrator.register_backend("MyBackend", lambda e, p: ([e, "run"], None, None), lambda b: "test.exe")
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
