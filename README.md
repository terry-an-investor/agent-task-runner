# agent-task-runner

PM-driven review-loop orchestrator for AI coding agents.

agent-task-runner runs a multi-round review loop: a **Worker** writes code, a **Reviewer** checks it, and the loop repeats until approval or max rounds. It ships with built-in support for **OpenAI Codex**, **Anthropic Claude**, and **OpenCode** as worker/reviewer backends, with automatic dispatch and real-time streaming output. Need something different? Use `register_backend()` to plug in your own — no core modifications required.

## Quick Start

```bash
# Install
pip install agent-task-runner
# or: uv add agent-task-runner

# Initialize loop directory in your project
loop init

# (optional) Build offline module map for the codebase
loop index

# Write a task card (see .loop/examples/task_card.json)
# Then run the loop with auto-dispatch
loop run --task .loop/task_card.json --auto-dispatch --worker-backend codex --reviewer-backend codex
```

## Prerequisites

- Python >= 3.11
- Git repository (the orchestrator uses git commits as the source of truth)
- At least one AI backend installed: [codex](https://github.com/openai/codex), [claude](https://docs.anthropic.com/en/docs/claude-code), or [opencode](https://opencode.ai)

## CLI Reference

```
loop init                  Create .loop/ directory structure and templates
loop index                 Build offline module map for src/loop_kit
loop run                   Run the full PM-controlled review loop
loop status                Show current loop state
loop health                Show worker/reviewer heartbeat health
loop heartbeat             Write role heartbeat continuously
loop archive               List or restore archived bus files
loop extract-diff BASE HEAD  Print git diff between two commits
```

### `loop run` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--task PATH` | `.loop/task_card.json` | Path to task card JSON |
| `--max-rounds N` | 3 | Maximum review rounds |
| `--timeout N` | 0 | Per-phase timeout in seconds (0=unlimited) |
| `--auto-dispatch` | off | Automatically invoke worker/reviewer backends each round |
| `--dispatch-backend native` | native | Subprocess transport |
| `--worker-backend codex\|claude\|opencode` | codex | Backend for worker dispatch |
| `--reviewer-backend codex\|claude\|opencode` | codex | Backend for reviewer dispatch |
| `--dispatch-timeout N` | 0 | Per-dispatch timeout in seconds (0=unlimited) |
| `--dispatch-retries N` | 2 | Retries on non-zero dispatch exit |
| `--dispatch-retry-base-sec N` | 5 | Base backoff seconds between dispatch retries (max delay 60s) |
| `--max-session-rounds N` | 0 | Max rounds to reuse one backend session before rotating (0 disables rotation) |
| `--artifact-timeout N` | 90 | Post-dispatch artifact wait in seconds |
| `--require-heartbeat` | off | Require live heartbeat while waiting |
| `--heartbeat-ttl N` | 30 | Heartbeat freshness threshold in seconds |
| `--single-round` | off | Run exactly one round and exit |
| `--round N` | - | Round number for single-round mode |
| `--resume` | off | Resume from .loop/state.json |
| `--reset` | off | Reset stale bus files before running |
| `--allow-dirty` | off | Allow starting with dirty tracked files |
| `--verbose` | off | Stream full backend stdout |
| `--loop-dir PATH` | .loop | Loop bus directory |

## File Bus Protocol

All state passes through JSON files in `.loop/`:

```
PM  -> Worker:    task_card.json / fix_list.json
Worker -> PM:     work_report.json
PM  -> Reviewer:  review_request.json
Reviewer -> PM:   review_report.json
```

### Context files

`loop init` also creates knowledge/context files in `.loop/context/`:

- **project_facts.md** — Project-specific facts and conventions
- **pitfalls.md** — Known pitfalls (auto-appended from review blocking issues)
- **patterns.jsonl** — High-confidence patterns (auto-populated from review issues on approval)
- **module_map.json** — Offline module index (populated by `loop index`)
- **handoff/** — Per-task role handoff artifacts (`.loop/handoff/{task_id}/{role}_r{round}.json`)

The worker first reads project `AGENTS.md` and `docs/roles/code-writer.md`, and the reviewer first reads project `docs/roles/reviewer.md`.
If any of those files are missing, agent-task-runner falls back to built-in defaults in `src/loop_kit/defaults/`.
Project files always override built-in defaults when present.

### Quickstart vs Handoff vs Warm Resume

- **Quickstart context**: injected for cold task starts (round 1) with stable project constraints and execution contract.
- **Handoff context**: structured bridge built every round for both roles (`done`, `open_questions`, `next_actions`, `evidence`, `must_read_files`) and injected into later prompts when available.
- **Warm resume**: reuses backend session IDs from `state.json` for low-latency continuation.
- **Session rotation**: set `--max-session-rounds` to intentionally start a fresh backend session after N rounds while preserving continuity via handoff context.
- **Fallback behavior**: invalid resume sessions are detected, logged, and retried with a fresh session.

### Key JSON schemas

**task_card.json**
```json
{
  "task_id": "T-001",
  "status": "todo",
  "goal": "One-sentence goal",
  "in_scope": ["file or module"],
  "out_of_scope": [],
  "acceptance_criteria": ["measurable criterion"],
  "constraints": []
}
```

`status` is system-managed while the loop runs: it is written to `in_progress` at start, `done` on approved completion, and `blocked` on non-approved terminal failures.

**work_report.json**
```json
{
  "task_id": "T-001",
  "round": 1,
  "head_sha": "abc123...",
  "files_changed": ["src/main.py"],
  "notes": "What was done",
  "tests": [{"name": "test_foo", "result": "pass"}]
}
```

**review_request.json**
```json
{
  "task_id": "T-001",
  "round": 1,
  "base_sha": "def456...",
  "head_sha": "abc123...",
  "commits": ["abc123 Fix foo"],
  "diff": "diff output...",
  "acceptance_criteria": ["measurable criterion"],
  "constraints": [],
  "worker_notes": "What was done",
  "worker_tests": [{"name": "test_foo", "result": "pass"}]
}
```

**fix_list.json**
```json
{
  "task_id": "T-001",
  "round": 2,
  "base_sha": "def456...",
  "head_sha": "abc123...",
  "fixes": [{"severity": "high", "file": "src/main.py", "reason": "..."}],
  "prior_round_notes": "What was done",
  "prior_review_non_blocking": ["..."]
}
```

**review_report.json**
```json
{
  "task_id": "T-001",
  "round": 1,
  "decision": "approve|changes_required",
  "blocking_issues": [{"severity": "high", "file": "src/main.py", "reason": "..."}],
  "non_blocking_suggestions": ["..."]
}
```

**state.json** — Internal orchestrator state, the single source of truth between rounds.

### Archive

All bus files are archived to `.loop/archive/{task_id}/r{N}_{name}.json` before being overwritten, preserving full round history.

## Architecture

```
                   ┌──────────┐
                   │   PM     │  orchestrator.py
                   │(outer)   │
                   └────┬─────┘
             ┌──────────┼──────────┐
             ▼          ▼          ▼
      ┌──────────┐ ┌──────────┐
      │  Worker  │ │ Reviewer │   (codex/claude/opencode subprocess)
      │(codex/   │ │(codex/   │
      │claude/   │ │claude/   │
      │opencode) │ │opencode) │
      └──────────┘ └──────────┘
```

Each round runs as a **fresh subprocess** (`python -m loop_kit run --single-round`), so code changes in the orchestrator itself take effect immediately — the orchestrator can improve itself.

Backend discovery uses `shutil.which()` plus known install paths. The backend registry (`register_backend()`) supports adding custom backends.

## Prompt Templates

`loop init` creates prompt templates in `.loop/templates/`:

- **worker_prompt.txt** — Worker prompt with `{task_id}`, `{round_num}`, `{agents_md}`, `{role_md}`, `{task_card_section}`, `{prior_context_section}`, `{work_report_path}` placeholders
- **reviewer_prompt.txt** — Reviewer prompt with `{task_id}`, `{round_num}`, `{role_md}`, `{review_report_path}` placeholders

## Development

```bash
# Clone and install editable
git clone <repo-url>
cd <repo-dir>
uv sync

# Run tests
uv run --group dev pytest

# Run as module
uv run python -m loop_kit init
```

## License

MIT
