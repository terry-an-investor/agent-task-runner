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
        _ = (
            task_path,
            max_rounds,
            timeout,
            require_heartbeat,
            heartbeat_ttl,
            auto_dispatch,
            dispatch_backend,
            dispatch_timeout,
            artifact_timeout,
            par_bin,
            par_worker_target,
            par_reviewer_target,
            single_round,
            round_num,
            allow_dirty,
            resume,
            verbose,
        )
        captured["worker_backend"] = worker_backend
        captured["reviewer_backend"] = reviewer_backend

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

    assert proc.kill_called is True
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

    assert proc.kill_called is True
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
    assert "Run loop init to create required files" in message


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
    assert "Run loop init to create required files" in message


def test_worker_prompt_raises_when_agents_doc_missing(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "AGENTS.md").unlink()

    with pytest.raises(RuntimeError) as exc:
        orchestrator._worker_prompt("T-613", 1)

    message = str(exc.value)
    assert "Missing required AGENTS.md" in message
    assert "Run loop init to create required files" in message


def test_reviewer_prompt_raises_when_role_doc_missing(tmp_path: Path, monkeypatch) -> None:
    _configure_loop_paths(monkeypatch, tmp_path)
    (tmp_path / "docs" / "roles" / "reviewer.md").unlink()

    with pytest.raises(RuntimeError) as exc:
        orchestrator._reviewer_prompt("T-613", 1)

    message = str(exc.value)
    assert "Missing required reviewer role doc" in message
    assert "Run loop init to create required files" in message


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
        _ = (
            task_path,
            max_rounds,
            timeout,
            require_heartbeat,
            heartbeat_ttl,
            auto_dispatch,
            dispatch_backend,
            worker_backend,
            reviewer_backend,
            dispatch_timeout,
            par_bin,
            par_worker_target,
            par_reviewer_target,
            allow_dirty,
            verbose,
        )
        captured["artifact_timeout"] = artifact_timeout
        captured["single_round"] = single_round
        captured["round_num"] = round_num
        captured["resume"] = resume
        captured["verbose"] = verbose

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
        str(task_path),
        3,
        0,
        False,
        orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
        False,
        "native",
        "codex",
        "codex",
        orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
        orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        "par",
        "worker",
        "reviewer",
        True,
        2,
        True,
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
        str(task_path),
        3,
        0,
        False,
        orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
        False,
        "native",
        "codex",
        "codex",
        orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
        orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        "par",
        "worker",
        "reviewer",
        True,
        1,
        True,
    )

    archived_state = json.loads(
        (orchestrator.LOOP_DIR / "archive" / "T-604" / "r1_state.json").read_text(encoding="utf-8")
    )
    assert archived_state["snapshot"] == "before-single-round-overwrite"


def test_cmd_run_exits_4_on_dirty_worktree(monkeypatch, capsys) -> None:
    monkeypatch.setattr(orchestrator, "_dirty_tracked_paths", lambda: ["tools/orchestrator.py"])

    with pytest.raises(SystemExit) as exc:
        orchestrator.cmd_run(
            ".loop/task_card.json",
            3,
            0,
            False,
            orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
            False,
            "native",
            "codex",
            "codex",
            orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
            orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
            "par",
            "worker",
            "reviewer",
            True,
            1,
            False,
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
        ".loop/task_card.json",
        3,
        0,
        False,
        orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
        False,
        "native",
        "codex",
        "codex",
        orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
        orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        "par",
        "worker",
        "reviewer",
        True,
        1,
        True,
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
        str(task_path),
        3,
        0,
        False,
        orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
        False,
        "native",
        "codex",
        "codex",
        orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
        orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        "par",
        "worker",
        "reviewer",
        True,
        1,
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
            str(task_path),
            3,
            0,
            False,
            orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
            False,
            "native",
            "codex",
            "codex",
            orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
            orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
            "par",
            "worker",
            "reviewer",
            True,
            1,
            True,
        )

    assert exc.value.code == 3
    state = json.loads(orchestrator.STATE_FILE.read_text(encoding="utf-8"))
    assert state["outcome"] == "invalid_work_report"
    assert "round" in state["error"]


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
        str(task_path),
        3,
        0,
        False,
        orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
        False,
        "native",
        "codex",
        "codex",
        orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
        orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        "par",
        "worker",
        "reviewer",
        True,
        1,
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
        str(task_path),
        3,
        30,
        True,
        40,
        True,
        "native",
        "codex",
        "claude",
        120,
        55,
        "par-bin",
        "worker-target",
        "reviewer-target",
        False,
        None,
    )

    assert len(calls) == 1
    cmd = calls[0]
    assert "--single-round" in cmd
    assert cmd[cmd.index("--round") + 1] == "1"
    assert "--auto-dispatch" in cmd
    assert cmd[cmd.index("--dispatch-backend") + 1] == "native"
    assert cmd[cmd.index("--worker-backend") + 1] == "codex"
    assert cmd[cmd.index("--reviewer-backend") + 1] == "claude"
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
        str(task_path),
        3,
        0,
        False,
        orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
        False,
        "native",
        "codex",
        "codex",
        orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
        orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        "par",
        "worker",
        "reviewer",
        False,
        None,
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
        str(task_path),
        3,
        0,
        False,
        orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
        False,
        "native",
        "codex",
        "codex",
        orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
        orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        "par",
        "worker",
        "reviewer",
        False,
        None,
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
        str(task_path),
        3,
        0,
        False,
        orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
        False,
        "native",
        "codex",
        "codex",
        orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
        orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        "par",
        "worker",
        "reviewer",
        False,
        None,
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
        str(task_path),
        3,
        0,
        False,
        orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
        False,
        "native",
        "codex",
        "codex",
        orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC,
        orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        "par",
        "worker",
        "reviewer",
        False,
        None,
        False,
        True,
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
        str(task_path), 3, 0, False, orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
        False, "native", "codex", "codex",
        orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC, orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
        "par", "worker", "reviewer", False, None, False, True,
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
            str(task_path), 3, 0, False, orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
            False, "native", "codex", "codex",
            orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC, orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
            "par", "worker", "reviewer", False, None, False, True,
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
            ".loop/task_card.json", 3, 0, False, orchestrator.DEFAULT_HEARTBEAT_TTL_SEC,
            False, "native", "codex", "codex",
            orchestrator.DEFAULT_DISPATCH_TIMEOUT_SEC, orchestrator.DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC,
            "par", "worker", "reviewer", False, None, False, False,
        )

    assert exc.value.code == 5
    err = capsys.readouterr().err
    assert "already running" in err
