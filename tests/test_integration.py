from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from loop_kit import orchestrator


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _git(tmp_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def _init_git_repo(tmp_path: Path) -> str:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Loop Integration Test")
    _git(tmp_path, "config", "user.email", "loop-integration@example.com")
    (tmp_path / "README.md").write_text("integration repo\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "chore: initial commit")
    return _git(tmp_path, "rev-parse", "HEAD")


def _write_loop_templates(loop_dir: Path) -> None:
    templates_dir = loop_dir / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / "worker_prompt.txt").write_text(
        orchestrator.DEFAULT_WORKER_PROMPT_TEMPLATE + "\n",
        encoding="utf-8",
    )
    (templates_dir / "reviewer_prompt.txt").write_text(
        orchestrator.DEFAULT_REVIEWER_PROMPT_TEMPLATE + "\n",
        encoding="utf-8",
    )


def _write_fake_opencode_backend(bin_dir: Path) -> Path:
    backend_script = bin_dir / "fake_opencode_backend.py"
    backend_script.write_text(
        (
            "from __future__ import annotations\n"
            "\n"
            "import json\n"
            "import os\n"
            "import re\n"
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            "\n"
            "# Force UTF-8 output on Windows where default console encoding may differ\n"
            'if hasattr(sys.stdout, "reconfigure"):\n'
            '    sys.stdout.reconfigure(encoding="utf-8")\n'
            'if hasattr(sys.stderr, "reconfigure"):\n'
            '    sys.stderr.reconfigure(encoding="utf-8")\n'
            "\n"
            "\n"
            "def _arg_value(name: str, argv: list[str]) -> str:\n"
            "    try:\n"
            "        i = argv.index(name)\n"
            "    except ValueError:\n"
            '        return ""\n'
            "    if i + 1 >= len(argv):\n"
            '        return ""\n'
            "    return argv[i + 1]\n"
            "\n"
            "\n"
            "def _prompt_value(pattern: str, prompt: str, default: str) -> str:\n"
            "    m = re.search(pattern, prompt)\n"
            "    if m is None:\n"
            "        return default\n"
            "    value = m.group(1).strip()\n"
            "    return value or default\n"
            "\n"
            "\n"
            "def _git(*args: str) -> str:\n"
            "    proc = subprocess.run(\n"
            '        ["git", *args],\n'
            "        cwd=Path.cwd(),\n"
            "        capture_output=True,\n"
            "        text=True,\n"
            '        encoding="utf-8",\n'
            "        check=True,\n"
            "    )\n"
            "    return proc.stdout.strip()\n"
            "\n"
            "\n"
            "def _emit_trace(trace_file: str, payload: dict[str, object]) -> None:\n"
            "    if not trace_file:\n"
            "        return\n"
            "    trace_path = Path(trace_file)\n"
            "    trace_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with trace_path.open('a', encoding='utf-8') as fh:\n"
            "        fh.write(json.dumps(payload, ensure_ascii=False) + '\\n')\n"
            "\n"
            "\n"
            "def main() -> int:\n"
            "    argv = sys.argv[1:]\n"
            '    mode = os.environ.get("FAKE_OPENCODE_MODE", "ok").strip().lower()\n'
            '    reviewer_decision = os.environ.get("FAKE_OPENCODE_REVIEW_DECISION", "approve").strip()\n'
            '    lane_review_decisions_raw = os.environ.get("FAKE_OPENCODE_LANE_REVIEW_DECISIONS", "").strip()\n'
            "    try:\n"
            "        lane_review_decisions = json.loads(lane_review_decisions_raw) if "
            "lane_review_decisions_raw else {}\n"
            "    except json.JSONDecodeError:\n"
            "        lane_review_decisions = {}\n"
            "    if not isinstance(lane_review_decisions, dict):\n"
            "        lane_review_decisions = {}\n"
            '    sleep_raw = os.environ.get("FAKE_OPENCODE_SLEEP_SEC", "0").strip()\n'
            "    try:\n"
            "        sleep_sec = float(sleep_raw) if sleep_raw else 0.0\n"
            "    except ValueError:\n"
            "        sleep_sec = 0.0\n"
            '    lane_conflict = os.environ.get("FAKE_OPENCODE_LANE_CONFLICT", "").strip().lower() '
            'in {"1", "true", "yes", "on"}\n'
            '    trace_file = os.environ.get("FAKE_OPENCODE_TRACE_FILE", "").strip()\n'
            '    timeout_race_role = os.environ.get("FAKE_OPENCODE_TIMEOUT_RACE_ROLE", "").strip().lower()\n'
            '    timeout_race_delay_raw = os.environ.get("FAKE_OPENCODE_TIMEOUT_RACE_DELAY_SEC", "").strip()\n'
            '    timeout_race_hold_raw = os.environ.get("FAKE_OPENCODE_TIMEOUT_RACE_HOLD_SEC", "").strip()\n'
            "    try:\n"
            "        timeout_race_delay_sec = float(timeout_race_delay_raw) if timeout_race_delay_raw else 0.0\n"
            "    except ValueError:\n"
            "        timeout_race_delay_sec = 0.0\n"
            "    try:\n"
            "        timeout_race_hold_sec = float(timeout_race_hold_raw) if timeout_race_hold_raw else 3.0\n"
            "    except ValueError:\n"
            "        timeout_race_hold_sec = 3.0\n"
            '    session_id = _arg_value("-s", argv) or "fake-session"\n'
            "\n"
            "    sys.stdout.write(\n"
            '        json.dumps({"type": "step_start", "part": {"sessionID": session_id}}) + "\\n"\n'
            "    )\n"
            "    sys.stdout.flush()\n"
            "\n"
            '    if mode == "fail":\n'
            '        sys.stderr.write("synthetic dispatch not found\\n")\n'
            "        sys.stderr.flush()\n"
            "        return 2\n"
            "\n"
            "    prompt = sys.stdin.read()\n"
            '    handoff_visible = "=== HANDOFF CONTEXT ===" in prompt and "role: " in prompt\n'
            '    quickstart_visible = "project_baseline:" in prompt\n'
            '    loop_dir = Path.cwd() / ".loop"\n'
            '    task_id = _prompt_value(r"Current task_id:\\s*([^,\\n]+)", prompt, "UNKNOWN")\n'
            '    if task_id == "UNKNOWN":\n'
            '        task_card_path = loop_dir / "task_card.json"\n'
            "        if task_card_path.exists():\n"
            "            try:\n"
            '                task_card = json.loads(task_card_path.read_text(encoding="utf-8"))\n'
            "            except json.JSONDecodeError:\n"
            "                task_card = {}\n"
            "            if isinstance(task_card, dict):\n"
            '                task_card_id = task_card.get("task_id")\n'
            "                if isinstance(task_card_id, str) and task_card_id.strip():\n"
            "                    task_id = task_card_id.strip()\n"
            '    round_text = _prompt_value(r"round:\\s*(\\d+)", prompt, "")\n'
            "    if not round_text.isdigit():\n"
            '        state_path = loop_dir / "state.json"\n'
            "        if state_path.exists():\n"
            "            try:\n"
            '                state_data = json.loads(state_path.read_text(encoding="utf-8"))\n'
            "            except json.JSONDecodeError:\n"
            "                state_data = {}\n"
            '            if isinstance(state_data, dict) and isinstance(state_data.get("round"), int):\n'
            '                round_text = str(state_data["round"])\n'
            "    if not round_text.isdigit():\n"
            '        round_text = "1"\n'
            "    round_num = int(round_text)\n"
            '    run_id = _prompt_value(r"run_id:\\s*([^,\\n]+)", prompt, "").strip().rstrip(".,;")\n'
            "    if not run_id:\n"
            '        state_path = loop_dir / "state.json"\n'
            "        if state_path.exists():\n"
            "            try:\n"
            '                state_data = json.loads(state_path.read_text(encoding="utf-8"))\n'
            "            except json.JSONDecodeError:\n"
            "                state_data = {}\n"
            '            state_run_id = state_data.get("run_id") if isinstance(state_data, dict) else None\n'
            "            if isinstance(state_run_id, str) and state_run_id.strip():\n"
            "                run_id = state_run_id.strip()\n"
            '    lane_id = _prompt_value(r"lane_id:\\s*([^\\n]+)", prompt, "").strip()\n'
            '    review_report_path = _prompt_value(r"after writing\\s+([^\\s]+)", prompt, "").strip().rstrip(".,;")\n'
            '    is_worker = "Role: code-writer worker for PM loop." in prompt\n'
            "    _emit_trace(\n"
            "        trace_file,\n"
            "        {\n"
            '            "event": "start",\n'
            '            "ts": time.time(),\n'
            '            "cwd": str(Path.cwd()),\n'
            '            "lane_id": lane_id,\n'
            '            "role": "worker" if is_worker else "reviewer",\n'
            "        },\n"
            "    )\n"
            "    if sleep_sec > 0:\n"
            "        time.sleep(sleep_sec)\n"
            "\n"
            "    if is_worker:\n"
            "        if lane_conflict and lane_id:\n"
            '            changed_file = Path.cwd() / "lane_conflict.txt"\n'
            '            changed_file.write_text(f"lane {lane_id} round {round_num}\\n", encoding="utf-8")\n'
            "        else:\n"
            "            if lane_id:\n"
            '                changed_name = f"{lane_id}_round_{round_num}.txt"\n'
            "            else:\n"
            '                changed_name = f"worker_round_{round_num}.txt"\n'
            "            changed_file = Path.cwd() / changed_name\n"
            '            changed_file.write_text(f"round {round_num}\\n", encoding="utf-8")\n'
            '        _git("add", changed_file.name)\n'
            '        _git("commit", "-m", f"worker round {round_num}")\n'
            '        head_sha = _git("rev-parse", "HEAD")\n'
            "        payload = {\n"
            '            "task_id": task_id,\n'
            '            "run_id": run_id,\n'
            '            "head_sha": head_sha,\n'
            '            "files_changed": [changed_file.name],\n'
            '            "tests": [{"name": "fake-opencode", "result": "pass", "output": "ok"}],\n'
            '            "notes": (\n'
            '                f"worker round {round_num} handoff_visible={handoff_visible} "\n'
            '                f"quickstart_visible={quickstart_visible}"\n'
            "            ),\n"
            '            "round": round_num,\n'
            "        }\n"
            '        target = loop_dir / "work_report.json"\n'
            "    else:\n"
            "        lane_decision = lane_review_decisions.get(lane_id) if lane_id else None\n"
            "        effective_reviewer_decision = (\n"
            "            lane_decision.strip()\n"
            "            if isinstance(lane_decision, str) and lane_decision.strip()\n"
            "            else reviewer_decision\n"
            "        )\n"
            "        payload = {\n"
            '            "task_id": task_id,\n'
            '            "run_id": run_id,\n'
            '            "decision": effective_reviewer_decision,\n'
            '            "blocking_issues": [],\n'
            '            "non_blocking_suggestions": [],\n'
            '            "round": round_num,\n'
            "        }\n"
            "        if review_report_path:\n"
            "            target = Path(review_report_path)\n"
            "            if not target.is_absolute():\n"
            "                target = Path.cwd() / target\n"
            "        else:\n"
            '            target = loop_dir / "review_report.json"\n'
            "\n"
            "    sys.stdout.write(\n"
            '        json.dumps({"type": "text", "part": {"text": "backend executing"}}) + "\\n"\n'
            "    )\n"
            "    sys.stdout.flush()\n"
            "\n"
            "    target.parent.mkdir(parents=True, exist_ok=True)\n"
            '    race_roles = {"both", "worker", "reviewer"}\n'
            "    race_enabled = (\n"
            "        timeout_race_delay_sec > 0\n"
            "        and timeout_race_role in race_roles\n"
            '        and (timeout_race_role == "both" or (timeout_race_role == "worker" and is_worker) or (timeout_race_role == "reviewer" and not is_worker))\n'
            "    )\n"
            "    if race_enabled:\n"
            "        payload_path = target.parent / (target.name + '.race_payload.json')\n"
            "        payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')\n"
            "        race_code = (\n"
            '            "import json,time,sys\\n"\n'
            '            "from pathlib import Path\\n"\n'
            "            \"payload_path = Path(sys.argv[1])\\n\"\n"
            "            \"target = Path(sys.argv[2])\\n\"\n"
            "            \"delay = float(sys.argv[3])\\n\"\n"
            "            \"time.sleep(delay)\\n\"\n"
            "            \"payload = json.loads(payload_path.read_text(encoding='utf-8'))\\n\"\n"
            "            \"target.parent.mkdir(parents=True, exist_ok=True)\\n\"\n"
            "            \"target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\\\\n', encoding='utf-8')\\n\"\n"
            "            \"payload_path.unlink(missing_ok=True)\\n\"\n"
            "        )\n"
            "        subprocess.Popen(\n"
            "            [sys.executable, '-c', race_code, str(payload_path), str(target), str(timeout_race_delay_sec)],\n"
            "            cwd=Path.cwd(),\n"
            "        )\n"
            "        time.sleep(max(timeout_race_hold_sec, timeout_race_delay_sec + 0.1))\n"
            "        return 0\n"
            '    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\\n", encoding="utf-8")\n'
            "\n"
            "    sys.stdout.write(\n"
            '        json.dumps({"type": "done", "part": {"artifact": str(target)}}) + "\\n"\n'
            "    )\n"
            "    sys.stdout.flush()\n"
            "    _emit_trace(\n"
            "        trace_file,\n"
            "        {\n"
            '            "event": "finish",\n'
            '            "ts": time.time(),\n'
            '            "cwd": str(Path.cwd()),\n'
            '            "lane_id": lane_id,\n'
            '            "role": "worker" if is_worker else "reviewer",\n'
            "        },\n"
            "    )\n"
            "\n"
            "    return 0\n"
            "\n"
            "\n"
            'if __name__ == "__main__":\n'
            "    sys.exit(main())\n"
        ),
        encoding="utf-8",
    )
    return backend_script


def _install_fake_opencode(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    backend_script = _write_fake_opencode_backend(bin_dir)

    # Windows launcher (`opencode.cmd`).
    (bin_dir / "opencode.cmd").write_text(
        f'@echo off\r\n"{sys.executable}" "{backend_script}" %*\r\n',
        encoding="utf-8",
    )

    # Unix launcher (`opencode`).
    unix_launcher = bin_dir / "opencode"
    unix_launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{backend_script}" "$@"\n',
        encoding="utf-8",
    )
    unix_launcher.chmod(unix_launcher.stat().st_mode | stat.S_IEXEC)


def _subprocess_env(
    tmp_path: Path,
    *,
    mode: str = "ok",
    reviewer_decision: str = "approve",
    lane_review_decisions: dict[str, str] | None = None,
    sleep_sec: float = 0.0,
    lane_conflict: bool = False,
    trace_file: Path | None = None,
    timeout_race_role: str | None = None,
    timeout_race_delay_sec: float = 0.0,
    timeout_race_hold_sec: float = 3.0,
) -> dict[str, str]:
    env = os.environ.copy()
    bin_dir = tmp_path / "bin"
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["FAKE_OPENCODE_MODE"] = mode
    env["FAKE_OPENCODE_REVIEW_DECISION"] = reviewer_decision
    if lane_review_decisions:
        env["FAKE_OPENCODE_LANE_REVIEW_DECISIONS"] = json.dumps(lane_review_decisions, ensure_ascii=False)
    else:
        env.pop("FAKE_OPENCODE_LANE_REVIEW_DECISIONS", None)
    env["FAKE_OPENCODE_SLEEP_SEC"] = str(sleep_sec)
    env["FAKE_OPENCODE_LANE_CONFLICT"] = "1" if lane_conflict else "0"
    if trace_file is not None:
        env["FAKE_OPENCODE_TRACE_FILE"] = str(trace_file)
    else:
        env.pop("FAKE_OPENCODE_TRACE_FILE", None)
    if isinstance(timeout_race_role, str) and timeout_race_role.strip():
        env["FAKE_OPENCODE_TIMEOUT_RACE_ROLE"] = timeout_race_role.strip().lower()
        env["FAKE_OPENCODE_TIMEOUT_RACE_DELAY_SEC"] = str(timeout_race_delay_sec)
        env["FAKE_OPENCODE_TIMEOUT_RACE_HOLD_SEC"] = str(timeout_race_hold_sec)
    else:
        env.pop("FAKE_OPENCODE_TIMEOUT_RACE_ROLE", None)
        env.pop("FAKE_OPENCODE_TIMEOUT_RACE_DELAY_SEC", None)
        env.pop("FAKE_OPENCODE_TIMEOUT_RACE_HOLD_SEC", None)
    # Force UTF-8 encoding for Python subprocesses on Windows
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    src_dir = Path(__file__).resolve().parents[1] / "src"
    current_py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(src_dir) if not current_py_path else str(src_dir) + os.pathsep + current_py_path
    return env


def _prepare_loop_contract(tmp_path: Path, *, task_id: str, base_sha: str, state_name: str, round_num: int) -> Path:
    loop_dir = tmp_path / ".loop"
    (loop_dir / "logs").mkdir(parents=True, exist_ok=True)
    (loop_dir / "runtime").mkdir(parents=True, exist_ok=True)
    (loop_dir / "archive").mkdir(parents=True, exist_ok=True)
    _write_loop_templates(loop_dir)

    _write_json(
        loop_dir / "task_card.json",
        {
            "task_id": task_id,
            "goal": "Integration test task",
            "in_scope": [],
            "out_of_scope": [],
            "acceptance_criteria": ["integration test"],
            "constraints": [],
        },
    )
    _write_json(
        loop_dir / "state.json",
        {
            "version": 1,
            "state": state_name,
            "round": round_num,
            "task_id": task_id,
            "base_sha": base_sha,
            "sessions": {},
        },
    )
    return loop_dir


def _run_loop(
    tmp_path: Path,
    args: list[str],
    *,
    mode: str = "ok",
    reviewer_decision: str = "approve",
    lane_review_decisions: dict[str, str] | None = None,
    sleep_sec: float = 0.0,
    lane_conflict: bool = False,
    trace_file: Path | None = None,
    timeout_race_role: str | None = None,
    timeout_race_delay_sec: float = 0.0,
    timeout_race_hold_sec: float = 3.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "loop_kit", *args],
        cwd=tmp_path,
        env=_subprocess_env(
            tmp_path,
            mode=mode,
            reviewer_decision=reviewer_decision,
            lane_review_decisions=lane_review_decisions,
            sleep_sec=sleep_sec,
            lane_conflict=lane_conflict,
            trace_file=trace_file,
            timeout_race_role=timeout_race_role,
            timeout_race_delay_sec=timeout_race_delay_sec,
            timeout_race_hold_sec=timeout_race_hold_sec,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _state_transition_pairs(feed_path: Path) -> list[tuple[str | None, str | None]]:
    pairs: list[tuple[str | None, str | None]] = []
    if not feed_path.exists():
        return pairs
    for raw in feed_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        data = json.loads(raw)
        if data.get("event") != "state_transition":
            continue
        payload = data.get("data")
        if not isinstance(payload, dict):
            continue
        pairs.append((payload.get("from_state"), payload.get("to_state")))
    return pairs


def _contains_ordered_pairs(
    actual: list[tuple[str | None, str | None]],
    expected: list[tuple[str | None, str | None]],
) -> bool:
    idx = 0
    for pair in actual:
        if pair == expected[idx]:
            idx += 1
            if idx == len(expected):
                return True
    return False


def _canonical_transition_labels(transitions: list[tuple[str | None, str | None]]) -> list[str]:
    label_map = {
        ("task_ready", "awaiting_work"): "task_ready -> work_done",
        ("awaiting_work", "awaiting_review"): "work_done -> review_done",
    }
    labels: list[str] = []
    for pair in transitions:
        label = label_map.get(pair)
        if label is not None:
            labels.append(label)
    return labels


@pytest.mark.timeout(10)
def test_full_worker_review_round(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-705",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )

    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "2",
        ],
        reviewer_decision="approve",
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert (loop_dir / "work_report.json").exists()
    assert (loop_dir / "review_report.json").exists()

    work = json.loads((loop_dir / "work_report.json").read_text(encoding="utf-8"))
    review = json.loads((loop_dir / "review_report.json").read_text(encoding="utf-8"))
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))

    assert work["task_id"] == "T-705"
    assert review["task_id"] == "T-705"
    assert state["state"] == "done"
    assert state["outcome"] == "approved"

    transitions = _state_transition_pairs(loop_dir / "logs" / "feed.jsonl")
    assert _contains_ordered_pairs(
        transitions,
        [
            ("task_ready", "awaiting_work"),
            ("awaiting_work", "awaiting_review"),
            ("awaiting_review", "done"),
        ],
    )
    assert _canonical_transition_labels(transitions) == [
        "task_ready -> work_done",
        "work_done -> review_done",
    ]


@pytest.mark.timeout(15)
def test_parallel_lane_dispatch_writes_lane_reports_and_merges_work_report(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-729",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )
    _write_json(
        loop_dir / "task_card.json",
        {
            "task_id": "T-729",
            "goal": "Parallel lane dispatch integration",
            "in_scope": [],
            "out_of_scope": [],
            "acceptance_criteria": ["parallel lanes"],
            "constraints": [],
            "lane_review_parallel": True,
            "lanes": [
                {"lane_id": "lane_core", "owner_paths": ["src/lane_core.py"]},
                {"lane_id": "lane_tests", "owner_paths": ["tests/lane_tests.py"]},
            ],
        },
    )
    trace_file = tmp_path / "dispatch_trace.jsonl"

    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "2",
            "--max-parallel-workers",
            "2",
        ],
        reviewer_decision="approve",
        sleep_sec=0.8,
        trace_file=trace_file,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    lane_core_report = loop_dir / "work_reports" / "lane_core.json"
    lane_tests_report = loop_dir / "work_reports" / "lane_tests.json"
    lane_core_review = loop_dir / "review_reports" / "lane_core.json"
    lane_tests_review = loop_dir / "review_reports" / "lane_tests.json"
    assert lane_core_report.exists()
    assert lane_tests_report.exists()
    assert lane_core_review.exists()
    assert lane_tests_review.exists()

    merged_work = json.loads((loop_dir / "work_report.json").read_text(encoding="utf-8"))
    assert merged_work["task_id"] == "T-729"
    assert merged_work["round"] == 1
    files_changed = merged_work.get("files_changed", [])
    assert "lane_core_round_1.txt" in files_changed
    assert "lane_tests_round_1.txt" in files_changed
    merge_provenance = merged_work.get("merge_provenance", {})
    assert merge_provenance["integration_lane_id"] == "__integration__"
    assert merge_provenance["strategy"] == "deterministic_v1_ordered_cherry_pick_rebase"
    assert merge_provenance["lane_execution_order"] == ["lane_core", "lane_tests"]
    assert [lane["lane_id"] for lane in merge_provenance["lanes"]] == ["lane_core", "lane_tests"]
    assert all(lane["status"] == "applied" for lane in merge_provenance["lanes"])
    assert all(check["result"] == "pass" for check in merge_provenance["acceptance_checks"])
    lane_metrics = merged_work.get("lane_metrics", [])
    assert {row["lane_id"]: row["review_decision"] for row in lane_metrics} == {
        "lane_core": "approve",
        "lane_tests": "approve",
    }
    integration_test_names = [
        item["name"] for item in merged_work.get("tests", []) if item["name"].startswith("integration/")
    ]
    assert set(integration_test_names) == {
        "integration/head_matches_merged_sha",
        "integration/merged_head_descends_from_base",
        "integration/provenance_order_matches_execution",
        "integration/worktree_clean_after_merge",
    }

    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["outcome"] == "approved"
    assert state["lanes"]["lane_core"]["status"] == "completed"
    assert state["lanes"]["lane_tests"]["status"] == "completed"
    assert state["lanes"]["lane_core"]["review_decision"] == "approve"
    assert state["lanes"]["lane_tests"]["review_decision"] == "approve"
    assert state["lanes"]["__integration__"]["status"] == "completed"

    trace_rows: list[dict[str, object]] = []
    for line in trace_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            trace_rows.append(payload)
    worker_starts = [row for row in trace_rows if row.get("event") == "start" and row.get("role") == "worker"]
    lane_worker_starts = [row for row in worker_starts if isinstance(row.get("lane_id"), str) and row["lane_id"]]
    assert lane_worker_starts
    assert {row["lane_id"] for row in lane_worker_starts}.issubset({"lane_core", "lane_tests"})
    if len(lane_worker_starts) >= 2:
        lane_worker_starts.sort(key=lambda row: float(row["ts"]))
        assert float(lane_worker_starts[1]["ts"]) - float(lane_worker_starts[0]["ts"]) < 0.5


@pytest.mark.timeout(15)
def test_parallel_lane_merge_conflict_fails_safe_with_integration_status(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-730",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )
    _write_json(
        loop_dir / "task_card.json",
        {
            "task_id": "T-730",
            "goal": "Parallel lane merge conflict",
            "in_scope": [],
            "out_of_scope": [],
            "acceptance_criteria": ["merge conflict should fail safely"],
            "constraints": [],
            "lanes": [
                {"lane_id": "lane_core", "owner_paths": ["src/lane_core.py"]},
                {"lane_id": "lane_tests", "owner_paths": ["tests/lane_tests.py"]},
            ],
        },
    )

    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "2",
            "--max-parallel-workers",
            "2",
        ],
        reviewer_decision="approve",
        lane_conflict=True,
    )

    assert result.returncode == orchestrator.EXIT_VALIDATION_ERROR
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["outcome"] == "lane_merge_failed"
    assert "Lane merge failed for lane" in str(state.get("error", ""))
    assert state["lanes"]["lane_core"]["status"] == "completed"
    assert state["lanes"]["lane_tests"]["status"] == "completed"
    assert state["lanes"]["__integration__"]["status"] == "failed"
    assert "Lane merge failed for lane" in str(state["lanes"]["__integration__"].get("error", ""))
    assert _git(tmp_path, "rev-parse", "HEAD") == base_sha
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no") == ""
    assert (loop_dir / "worktrees" / "T-730" / "1" / "lane_core").exists()
    assert (loop_dir / "worktrees" / "T-730" / "1" / "lane_tests").exists()


@pytest.mark.timeout(15)
def test_parallel_lane_merge_conflict_skip_lane_policy_completes(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-731",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )
    _write_json(
        loop_dir / "task_card.json",
        {
            "task_id": "T-731",
            "goal": "Parallel lane merge conflict skip policy",
            "in_scope": [],
            "out_of_scope": [],
            "acceptance_criteria": ["skip conflicting lane and continue"],
            "constraints": [],
            "lane_merge_conflict_policy": "skip_lane",
            "lanes": [
                {"lane_id": "lane_core", "owner_paths": ["src/lane_core.py"]},
                {"lane_id": "lane_tests", "owner_paths": ["tests/lane_tests.py"]},
            ],
        },
    )

    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "2",
            "--max-parallel-workers",
            "2",
        ],
        reviewer_decision="approve",
        lane_conflict=True,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    merged_work = json.loads((loop_dir / "work_report.json").read_text(encoding="utf-8"))
    merge_provenance = merged_work.get("merge_provenance", {})
    lane_status_by_id = {lane["lane_id"]: lane["status"] for lane in merge_provenance.get("lanes", [])}
    assert lane_status_by_id == {
        "lane_core": "applied",
        "lane_tests": "skipped_conflict",
    }
    preflight = merge_provenance.get("preflight", {})
    assert preflight.get("policy") == "skip_lane"
    assert preflight.get("conflicts")
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["outcome"] == "approved"
    assert state["lanes"]["__integration__"]["status"] == "completed"


@pytest.mark.timeout(15)
def test_parallel_lane_merge_conflict_defer_lane_persistent_conflict_fails(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-731d",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )
    _write_json(
        loop_dir / "task_card.json",
        {
            "task_id": "T-731d",
            "goal": "Parallel lane merge conflict defer policy",
            "in_scope": [],
            "out_of_scope": [],
            "acceptance_criteria": ["deferred conflicts must fail integration"],
            "constraints": [],
            "lane_merge_conflict_policy": "defer_lane",
            "lanes": [
                {"lane_id": "lane_core", "owner_paths": ["src/lane_core.py"]},
                {"lane_id": "lane_tests", "owner_paths": ["tests/lane_tests.py"]},
            ],
        },
    )

    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "2",
            "--max-parallel-workers",
            "2",
        ],
        reviewer_decision="approve",
        lane_conflict=True,
    )

    assert result.returncode == orchestrator.EXIT_VALIDATION_ERROR
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["outcome"] == "lane_merge_failed"
    assert "deferred replay conflicts" in str(state.get("error", ""))
    assert state["lanes"]["__integration__"]["status"] == "failed"
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no") == ""


@pytest.mark.timeout(15)
def test_parallel_lane_merge_conflict_cleans_worktrees_when_preserve_disabled(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-732",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )
    _write_json(
        loop_dir / "task_card.json",
        {
            "task_id": "T-732",
            "goal": "Parallel lane merge conflict cleanup",
            "in_scope": [],
            "out_of_scope": [],
            "acceptance_criteria": ["cleanup lane worktrees when not preserving"],
            "constraints": [],
            "lane_merge_conflict_policy": "fail_fast",
            "lane_preserve_worktrees_on_failure": False,
            "lanes": [
                {"lane_id": "lane_core", "owner_paths": ["src/lane_core.py"]},
                {"lane_id": "lane_tests", "owner_paths": ["tests/lane_tests.py"]},
            ],
        },
    )

    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "2",
            "--max-parallel-workers",
            "2",
        ],
        reviewer_decision="approve",
        lane_conflict=True,
    )

    assert result.returncode == orchestrator.EXIT_VALIDATION_ERROR
    assert _git(tmp_path, "rev-parse", "HEAD") == base_sha
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no") == ""
    assert not (loop_dir / "worktrees" / "T-732" / "1" / "lane_core").exists()
    assert not (loop_dir / "worktrees" / "T-732" / "1" / "lane_tests").exists()


@pytest.mark.timeout(15)
def test_parallel_lane_review_gate_rejects_before_integration(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-733",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )
    _write_json(
        loop_dir / "task_card.json",
        {
            "task_id": "T-733",
            "goal": "Parallel lane review gate reject",
            "in_scope": [],
            "out_of_scope": [],
            "acceptance_criteria": ["lane review gate"],
            "constraints": [],
            "lane_review_parallel": True,
            "lanes": [
                {"lane_id": "lane_core", "owner_paths": ["src/lane_core.py"]},
                {"lane_id": "lane_tests", "owner_paths": ["tests/lane_tests.py"]},
            ],
        },
    )

    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "2",
            "--max-parallel-workers",
            "2",
        ],
        reviewer_decision="approve",
        lane_review_decisions={"lane_tests": "changes_required"},
    )

    assert result.returncode == orchestrator.EXIT_VALIDATION_ERROR
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["outcome"] == "lane_review_rejected"
    assert state["lanes"]["lane_core"]["review_decision"] == "approve"
    assert state["lanes"]["lane_tests"]["review_decision"] == "changes_required"
    assert "__integration__" not in state["lanes"]
    assert (loop_dir / "review_report.json").exists() is False


@pytest.mark.timeout(10)
def test_session_resume_across_rounds(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-705",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )

    round1 = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "2",
        ],
        reviewer_decision="changes_required",
    )
    assert round1.returncode == 0, f"stdout:\n{round1.stdout}\nstderr:\n{round1.stderr}"

    state_after_round1 = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state_after_round1["state"] == "awaiting_work"
    assert state_after_round1["round"] == 2
    worker_sid = state_after_round1["sessions"]["worker"]["session_id"]

    resumed = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--resume",
            "--max-rounds",
            "2",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "2",
        ],
        reviewer_decision="approve",
    )
    assert resumed.returncode == 0, f"stdout:\n{resumed.stdout}\nstderr:\n{resumed.stderr}"

    state_after_resume = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state_after_resume["state"] == "done"
    assert state_after_resume["outcome"] == "approved"
    assert state_after_resume["sessions"]["worker"]["session_id"] == worker_sid

    worker_dispatch_log = (loop_dir / "logs" / "worker_dispatch.log").read_text(encoding="utf-8")
    assert f"session_id={worker_sid}" in worker_dispatch_log
    assert f"-s {worker_sid}" in worker_dispatch_log


@pytest.mark.timeout(10)
def test_dispatch_failure_handling(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-705",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )

    failed = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "2",
        ],
        mode="fail",
    )
    assert failed.returncode == orchestrator.EXIT_VALIDATION_ERROR

    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["outcome"] == "worker_dispatch_failed"
    assert "dispatch failed" in str(state.get("error", "")).lower()


@pytest.mark.timeout(10)
def test_report_rejects_cross_task_archived_review_artifact(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-705",
        base_sha=base_sha,
        state_name="done",
        round_num=1,
    )
    archive_dir = loop_dir / "archive" / "T-705"
    archive_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        archive_dir / "r1_review_report.json",
        {
            "task_id": "T-999",
            "round": 1,
            "decision": "approve",
        },
    )

    result = subprocess.run(
        [sys.executable, "-m", "loop_kit", "report", "--loop-dir", ".loop", "--task-id", "T-705", "--format", "json"],
        cwd=tmp_path,
        env=_subprocess_env(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == orchestrator.EXIT_VALIDATION_ERROR
    assert "field 'task_id' mismatch" in result.stderr


@pytest.mark.timeout(15)
def test_auto_dispatch_worker_timeout_race_accepts_matching_artifact(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-737-race",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )

    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-timeout",
            "1",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "4",
        ],
        reviewer_decision="approve",
        timeout_race_role="worker",
        timeout_race_delay_sec=1.1,
        timeout_race_hold_sec=3.0,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    work = json.loads((loop_dir / "work_report.json").read_text(encoding="utf-8"))
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["outcome"] == "approved"
    assert work["run_id"] == state["run_id"]


@pytest.mark.timeout(15)
def test_auto_dispatch_reviewer_timeout_race_accepts_matching_artifact(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-737-race-reviewer",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )

    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-timeout",
            "1",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "4",
        ],
        reviewer_decision="approve",
        timeout_race_role="reviewer",
        timeout_race_delay_sec=1.1,
        timeout_race_hold_sec=3.0,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    review = json.loads((loop_dir / "review_report.json").read_text(encoding="utf-8"))
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["outcome"] == "approved"
    assert review["run_id"] == state["run_id"]


@pytest.mark.timeout(15)
def test_auto_dispatch_ignores_stale_cross_run_work_report(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    _install_fake_opencode(tmp_path / "bin")
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-737-stale",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )

    def _inject_stale_work_report() -> None:
        log_path = loop_dir / "logs" / "orchestrator.log"
        deadline = time.time() + 4.0
        while time.time() < deadline:
            if log_path.exists():
                try:
                    log_text = log_path.read_text(encoding="utf-8")
                except OSError:
                    log_text = ""
                if "Waiting for work_report.json" in log_text:
                    break
            time.sleep(0.05)
        _write_json(
            loop_dir / "work_report.json",
            {
                "task_id": "T-737-stale",
                "run_id": "run-stale",
                "round": 1,
                "head_sha": base_sha,
                "files_changed": [],
                "tests": [],
                "notes": "stale injected artifact",
            },
        )

    injector = threading.Thread(target=_inject_stale_work_report, daemon=True)
    injector.start()
    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--auto-dispatch",
            "--worker-backend",
            "opencode",
            "--reviewer-backend",
            "opencode",
            "--dispatch-retries",
            "0",
            "--artifact-timeout",
            "4",
        ],
        reviewer_decision="approve",
        sleep_sec=1.0,
    )
    injector.join(timeout=1)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    work = json.loads((loop_dir / "work_report.json").read_text(encoding="utf-8"))
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["outcome"] == "approved"
    assert work["run_id"] == state["run_id"]
    assert work["run_id"] != "run-stale"


@pytest.mark.timeout(15)
def test_single_round_no_change_success_exits_without_reviewer(tmp_path: Path) -> None:
    base_sha = _init_git_repo(tmp_path)
    loop_dir = _prepare_loop_contract(
        tmp_path,
        task_id="T-744-noop",
        base_sha=base_sha,
        state_name="task_ready",
        round_num=1,
    )
    state_payload = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    state_payload["run_id"] = "run-noop-success"
    _write_json(loop_dir / "state.json", state_payload)

    def _inject_work_report() -> None:
        log_path = loop_dir / "logs" / "orchestrator.log"
        deadline = time.time() + 4.0
        while time.time() < deadline:
            if log_path.exists():
                try:
                    log_text = log_path.read_text(encoding="utf-8")
                except OSError:
                    log_text = ""
                if "Waiting for work_report.json" in log_text:
                    break
            time.sleep(0.05)
        _write_json(
            loop_dir / "work_report.json",
            {
                "task_id": "T-744-noop",
                "run_id": "run-noop-success",
                "round": 1,
                "head_sha": "HEAD",
                "files_changed": [],
                "tests": [{"name": "manual", "result": "pass", "output": "noop"}],
                "notes": "no-op integration test",
            },
        )

    injector = threading.Thread(target=_inject_work_report, daemon=True)
    injector.start()
    result = _run_loop(
        tmp_path,
        [
            "run",
            "--loop-dir",
            ".loop",
            "--task",
            ".loop/task_card.json",
            "--single-round",
            "--round",
            "1",
            "--worker-noop-as-success",
            "--timeout",
            "5",
        ],
    )
    injector.join(timeout=1)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
    work = json.loads((loop_dir / "work_report.json").read_text(encoding="utf-8"))
    summary = json.loads((loop_dir / "summary.json").read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["outcome"] == "no_change_success"
    assert work["head_sha"] == base_sha
    assert summary["outcome"] == "no_change_success"
    assert (loop_dir / "review_request.json").exists() is False
    assert (loop_dir / "review_report.json").exists() is False
