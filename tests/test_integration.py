from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
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
            "from pathlib import Path\n"
            "\n"
            "\n"
            "def _arg_value(name: str, argv: list[str]) -> str:\n"
            "    try:\n"
            "        i = argv.index(name)\n"
            "    except ValueError:\n"
            "        return \"\"\n"
            "    if i + 1 >= len(argv):\n"
            "        return \"\"\n"
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
            "        [\"git\", *args],\n"
            "        cwd=Path.cwd(),\n"
            "        capture_output=True,\n"
            "        text=True,\n"
            "        encoding=\"utf-8\",\n"
            "        check=True,\n"
            "    )\n"
            "    return proc.stdout.strip()\n"
            "\n"
            "\n"
            "def main() -> int:\n"
            "    argv = sys.argv[1:]\n"
            "    mode = os.environ.get(\"FAKE_OPENCODE_MODE\", \"ok\").strip().lower()\n"
            "    reviewer_decision = os.environ.get(\"FAKE_OPENCODE_REVIEW_DECISION\", \"approve\").strip()\n"
            "    session_id = _arg_value(\"-s\", argv) or \"fake-session\"\n"
            "\n"
            "    sys.stdout.write(json.dumps({\"type\": \"step_start\", \"part\": {\"sessionID\": session_id}}) + \"\\n\")\n"
            "    sys.stdout.flush()\n"
            "\n"
            "    if mode == \"fail\":\n"
            "        sys.stderr.write(\"synthetic dispatch not found\\n\")\n"
            "        sys.stderr.flush()\n"
            "        return 2\n"
            "\n"
            "    prompt = sys.stdin.read()\n"
            "    handoff_visible = \"=== HANDOFF CONTEXT ===\" in prompt and \"role: \" in prompt\n"
            "    quickstart_visible = \"project_baseline:\" in prompt\n"
            "    loop_dir = Path.cwd() / \".loop\"\n"
            "    task_id = _prompt_value(r\"Current task_id:\\\\s*([^,\\\\n]+)\", prompt, \"UNKNOWN\")\n"
            "    if task_id == \"UNKNOWN\":\n"
            "        task_card_path = loop_dir / \"task_card.json\"\n"
            "        if task_card_path.exists():\n"
            "            try:\n"
            "                task_card = json.loads(task_card_path.read_text(encoding=\"utf-8\"))\n"
            "            except json.JSONDecodeError:\n"
            "                task_card = {}\n"
            "            if isinstance(task_card, dict):\n"
            "                task_card_id = task_card.get(\"task_id\")\n"
            "                if isinstance(task_card_id, str) and task_card_id.strip():\n"
            "                    task_id = task_card_id.strip()\n"
            "    round_text = _prompt_value(r\"round:\\\\s*(\\\\d+)\", prompt, \"\")\n"
            "    if not round_text.isdigit():\n"
            "        state_path = loop_dir / \"state.json\"\n"
            "        if state_path.exists():\n"
            "            try:\n"
            "                state_data = json.loads(state_path.read_text(encoding=\"utf-8\"))\n"
            "            except json.JSONDecodeError:\n"
            "                state_data = {}\n"
            "            if isinstance(state_data, dict) and isinstance(state_data.get(\"round\"), int):\n"
            "                round_text = str(state_data[\"round\"])\n"
            "    if not round_text.isdigit():\n"
            "        round_text = \"1\"\n"
            "    round_num = int(round_text)\n"
            "\n"
            "    if \"Role: code-writer worker for PM loop.\" in prompt:\n"
            "        changed_file = Path.cwd() / f\"worker_round_{round_num}.txt\"\n"
            "        changed_file.write_text(f\"round {round_num}\\\\n\", encoding=\"utf-8\")\n"
            "        _git(\"add\", changed_file.name)\n"
            "        _git(\"commit\", \"-m\", f\"worker round {round_num}\")\n"
            "        head_sha = _git(\"rev-parse\", \"HEAD\")\n"
            "        payload = {\n"
            "            \"task_id\": task_id,\n"
            "            \"head_sha\": head_sha,\n"
            "            \"files_changed\": [changed_file.name],\n"
            "            \"tests\": [{\"name\": \"fake-opencode\", \"result\": \"pass\", \"output\": \"ok\"}],\n"
            "            \"notes\": f\"worker round {round_num} handoff_visible={handoff_visible} quickstart_visible={quickstart_visible}\",\n"
            "            \"round\": round_num,\n"
            "        }\n"
            "        target = loop_dir / \"work_report.json\"\n"
            "    else:\n"
            "        payload = {\n"
            "            \"task_id\": task_id,\n"
            "            \"decision\": reviewer_decision,\n"
            "            \"blocking_issues\": [],\n"
            "            \"non_blocking_suggestions\": [],\n"
            "            \"round\": round_num,\n"
            "        }\n"
            "        target = loop_dir / \"review_report.json\"\n"
            "\n"
            "    sys.stdout.write(json.dumps({\"type\": \"text\", \"part\": {\"text\": \"backend executing\"}}) + \"\\n\")\n"
            "    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + \"\\n\", encoding=\"utf-8\")\n"
            "    sys.stdout.write(json.dumps({\"type\": \"step_finish\", \"part\": {}}) + \"\\n\")\n"
            "    sys.stdout.flush()\n"
            "    return 0\n"
            "\n"
            "\n"
            "if __name__ == \"__main__\":\n"
            "    raise SystemExit(main())\n"
        ),
        encoding="utf-8",
    )
    return backend_script


def _install_fake_opencode(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    backend_script = _write_fake_opencode_backend(bin_dir)

    # Windows launcher (`opencode.cmd`).
    (bin_dir / "opencode.cmd").write_text(
        f"@echo off\r\n\"{sys.executable}\" \"{backend_script}\" %*\r\n",
        encoding="utf-8",
    )

    # Unix launcher (`opencode`).
    unix_launcher = bin_dir / "opencode"
    unix_launcher.write_text(
        f"#!/bin/sh\nexec \"{sys.executable}\" \"{backend_script}\" \"$@\"\n",
        encoding="utf-8",
    )
    unix_launcher.chmod(unix_launcher.stat().st_mode | stat.S_IEXEC)


def _subprocess_env(tmp_path: Path, *, mode: str = "ok", reviewer_decision: str = "approve") -> dict[str, str]:
    env = os.environ.copy()
    bin_dir = tmp_path / "bin"
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["FAKE_OPENCODE_MODE"] = mode
    env["FAKE_OPENCODE_REVIEW_DECISION"] = reviewer_decision

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


def _run_loop(tmp_path: Path, args: list[str], *, mode: str = "ok", reviewer_decision: str = "approve") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "loop_kit", *args],
        cwd=tmp_path,
        env=_subprocess_env(tmp_path, mode=mode, reviewer_decision=reviewer_decision),
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


def _contains_ordered_pairs(actual: list[tuple[str | None, str | None]], expected: list[tuple[str | None, str | None]]) -> bool:
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
