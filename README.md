# AI Code Review Orchestrator | agent-task-runner

[![ci](../../actions/workflows/loop-ci.yml/badge.svg)](../../actions/workflows/loop-ci.yml)

**AI-driven automated code review and development workflow orchestration**

agent-task-runner automates the complete development loop: a **Worker AI writes code**, a **Reviewer AI validates it**, and the system iterates automatically until approval or max rounds reached. Built for teams using AI coding assistants (OpenAI Codex, Anthropic Claude, OpenCode) who want structured, auditable, and efficient code review processes.

## ✨ Why Use This?

- **Automated code review cycles** - No manual back-and-forth between writing and reviewing
- **Multi-round iteration** - Systematically handles "changes required" feedback
- **Smart context management** - Retrieves relevant project facts, patterns, and pitfalls per task
- **Full observability** - Track latency metrics, round history, and decision trails
- **Extensible by design** - Plug in any AI backend via `register_backend()` without core changes
- **Git-integrated** - Uses commits as source of truth, works with existing workflows

## Quick Start

```bash
# Initialize loop directory in your project
loop init

# (optional) Build offline module map for faster context
loop index

# Create task card (see example below)
# Run with auto-dispatch
loop run --task .loop/task_card.json --auto-dispatch --worker-backend codex --reviewer-backend codex
```

**Example task card** (`.loop/task_card.json`):

```json
{
  "task_id": "T-001",
  "status": "todo",
  "goal": "Add input validation to user registration endpoint",
  "in_scope": ["src/api/auth.py", "tests/test_auth.py"],
  "out_of_scope": ["UI changes"],
  "acceptance_criteria": [
    "Email format validated",
    "Password strength enforced",
    "Tests cover edge cases"
  ],
  "lanes": [
    {
      "lane_id": "lane_core",
      "owner_paths": ["src/api/auth.py"],
      "depends_on": [],
      "backend_preference": "codex"
    }
  ]
}
```

Run and watch rounds progress in `.loop/state.json`.

## Prerequisites

- Python >= 3.11
- Git repository (the orchestrator uses git commits as the source of truth)
- At least one AI backend installed: [codex](https://github.com/openai/codex), [claude](https://docs.anthropic.com/en/docs/claude-code), or [opencode](https://opencode.ai)

## 📋 Contents

- [CLI Reference](#cli-reference)
- [Core Concepts](#core-concepts)
- [Architecture](#architecture)
- [Typical Workflow](#typical-workflow)
- [Performance](#performance)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Typical Workflow

1. **Create task card** — Define goal, scope, acceptance criteria
2. **Run loop** — System executes rounds automatically:
   - Worker writes code based on task card
   - Reviewer validates against criteria
   - On `changes_required`, loop repeats (max rounds)
3. **Monitor** — Check `.loop/state.json` and `.loop/logs/` for progress
4. **Approve or fix** — Loop ends when reviewer approves or max rounds exhausted

```
Round 1: Worker → Reviewer → changes_required
Round 2: Worker → Reviewer → approve ✅
```

Key files:
- `.loop/state.json` — Current state and round number
- `.loop/logs/feed.jsonl` — Event log for debugging
- `.loop/archive/{task_id}/` — Historical artifacts per round

## CLI Reference

```
loop init                  Create .loop/ directory structure and templates
loop index                 Build offline module map for src/loop_kit
loop run                   Run the full PM-controlled review loop
loop knowledge             Manage built-in defaults knowledge JSONL files
loop status                Show current loop state (use --tree for dependency DAG view)
loop health                Show worker/reviewer heartbeat health
loop dispatch-metrics      Summarize dispatch phase latency metrics from feed logs
loop heartbeat             Write role heartbeat continuously
loop archive               List or restore archived bus files
loop extract-diff BASE HEAD  Print git diff between two commits
loop diff                  Compare archived artifacts between two rounds
loop report                Summarize task progress from state/archive artifacts
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

### `loop status` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--tree` | off | Render dependency tree from `task_card.json` and show blocked reasons per node |
| `--loop-dir PATH` | .loop | Loop bus directory |

### `loop dispatch-metrics` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--task-id ID` | all task IDs | Filter `dispatch_phase_metrics` rows by task ID |
| `--role all\|worker\|reviewer` | all | Filter rows by role |
| `--loop-dir PATH` | .loop | Loop bus directory |

Example:

```bash
loop dispatch-metrics --task-id T-715 --role worker
```

### `loop diff` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--task-id ID` | required | Task ID archive key (for example `T-604`) |
| `--base-round N` | required | Base archive round number (`>=1`) |
| `--head-round N` | required | Head archive round number (`>=1`) |
| `--artifact all\|state\|work_report\|review_report` | all | Which archived artifact to diff |
| `--loop-dir PATH` | .loop | Loop bus directory |

The command compares archived `state`, `work_report`, and/or `review_report` JSON artifacts between two rounds and prints a deterministic unified diff. Missing rounds, missing required artifacts, or archive identity mismatches (`task_id`/`round` not matching filename routing) fail fast with a validation error.

### `loop report` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--task-id ID` | from `state.json` | Task ID to summarize |
| `--format json\|markdown` | json | Output format |
| `--loop-dir PATH` | .loop | Loop bus directory |

`--format markdown` renders a deterministic task summary including goal, status, rounds, per-round decisions, and changed files when available. The report command only consumes artifacts whose payload identity matches the requested task and round; inconsistent archived artifacts fail with validation errors.

`loop knowledge` manages built-in defaults:

- `list [--category CAT]` — Print facts, pitfalls, patterns
- `add --pattern TEXT --category CAT --confidence 0..1 --source ORIGIN` — Append pattern
- `prune --older-than DAYS` — Remove stale entries
- `dedupe` — Remove duplicates

All writes are atomic and modify `src/loop_kit/defaults/*.jsonl`.

### Configuration files and env vars

`loop run` also reads defaults from `.loop/config.yaml` (preferred) or `.loop/config.json` (backward compatible).

- YAML support is optional and uses `PyYAML` when installed. If `config.yaml` exists but `PyYAML` is unavailable, it is skipped with a warning.
- Existing `.loop/config.json` files continue to work unchanged.

Environment variable overrides:

- `LOOP_MAX_ROUNDS` -> `RunConfig.max_rounds`
- `LOOP_DISPATCH_TIMEOUT` -> `RunConfig.dispatch_timeout`
- `LOOP_BACKEND_PREFERENCE` -> `RunConfig.backend_preference` (comma-separated, e.g. `codex,claude,opencode`)

Resolution order:

`CLI args > environment variables > config file > built-in defaults`

## Architecture

**task_card.json**
```json
{
  "task_id": "T-001",
  "status": "todo",
  "goal": "One-sentence goal",
  "in_scope": ["file or module"],
  "out_of_scope": [],
  "acceptance_criteria": ["measurable criterion"],
  "depends_on": ["T-000"],
  "lanes": [
    {
      "lane_id": "lane_core",
      "owner_paths": ["src/loop_kit/orchestrator.py"],
      "depends_on": [],
      "backend_preference": "codex",
      "acceptance_checks": ["unit tests pass for lane-owned files"]
    }
  ],
  "constraints": []
}
```

`depends_on` is optional. A legacy alias field `dependencies` is also accepted.

`status` is system-managed: `in_progress` at start, `done` on approval, `blocked` on failures.

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

### Architecture Overview

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

Each round runs as a fresh subprocess (`python -m loop_kit run --single-round`), so code changes take effect immediately.

Backend discovery uses `shutil.which()` plus known install paths. The backend registry (`register_backend()`) supports adding custom backends.

## Core Concepts

### Worker-Reviewer Loop

The orchestrator coordinates two AI agents:

1. **Worker** - Writes/updates code based on task requirements
2. **Reviewer** - Validates against acceptance criteria, provides feedback

The loop continues until:
- ✅ Reviewer approves
- ⏰ Max rounds exhausted
- ❌ Non-recoverable error

### File Bus Protocol

All communication uses JSON files in `.loop/`:

```
PM → Worker:   task_card.json / fix_list.json
Worker → PM:   work_report.json
PM → Reviewer: review_request.json
Reviewer → PM:   review_report.json
```

### Knowledge System

- **Facts** - Project-specific conventions (`project_facts.md`)
- **Pitfalls** - Known issues to avoid (`pitfalls.md`)
- **Patterns** - High-confidence coding patterns (`patterns.jsonl`)
- **Module Map** - Offline codebase index (`module_map.json`)

Context is **retrieved by relevance** (keyword scoring) rather than injected wholesale, keeping prompts focused.

### Core Run-Loop State Machine

The orchestrator uses explicit state metadata and trigger-driven transitions:

- `idle` - No active contract
- `awaiting_work` - Worker phase
- `awaiting_review` - Reviewer phase
- `done` - Terminal (approved, timeout, or blocked)

Transitions are trigger-driven:

| Trigger | Transition | Kind |
|---------|------------|------|
| `bootstrap` | any → `awaiting_work` | normal |
| `worker_completed` | `awaiting_work` → `awaiting_review` | normal |
| `reviewer_approved` | `awaiting_review` → `done` | normal |
| `reviewer_changes_required` | `awaiting_review` → `awaiting_work` | retry |
| `*_timeout` | → `done` | timeout |
| `terminal_error` | any → `done` | error |
| `max_rounds_exhausted` | → `done` | retry |

### Quickstart vs Handoff vs Warm Resume

- **Quickstart context**: injected for cold task starts (round 1)
- **Handoff context**: structured bridge built every round for both roles (`done`, `open_questions`, `next_actions`, `evidence`, `must_read_files`)
- **Warm resume**: reuses backend session IDs from `state.json` for low-latency continuation
- **Session rotation**: set `--max-session-rounds` to intentionally rotate sessions
- **Fallback**: invalid resume sessions are detected and retried with fresh sessions

## Performance Metrics

`loop dispatch-metrics` analyzes `.loop/logs/feed.jsonl` to report:

- **Phase latencies**: `startup_ms`, `context_to_work_ms`, `work_to_artifact_ms`, `total_ms`
- **Work subphases**: `read/search/edit/test/unknown` (counts & durations)

Interpretation:
- `startup_ms`: process startup + first output
- `context_to_work_ms`: initial prompt processing before execution
- `work_to_artifact_ms`: actual code work duration

### Optimization Tips

**Slow startup** (`startup_ms` high):
- Use `--max-session-rounds N` to reuse backend sessions
- Pre-index with `loop index` for faster context

**Slow work phase** (`work_to_artifact_ms` high):
- Narrow `in_scope` files in task card
- Add precise `acceptance_criteria` to reduce back-and-forth
- Consider splitting into multiple lanes

**High retry count**:
- Improve acceptance criteria clarity
- Add more project facts via `loop knowledge add`
- Tune prompt templates in `.loop/templates/`

Monitor with: `loop dispatch-metrics --task-id <id> --role worker`

### Best Practices

**Task cards**
- One clear goal per task (avoid scope creep)
- Specific, testable acceptance criteria
- Use `lanes` for multi-module changes to isolate worktrees

**Backend choice**
- `codex`: Fast, good for boilerplate
- `claude`: Strong reasoning, complex refactors
- `opencode`: Local, no API costs

**Lanes**
- Enable for changes spanning multiple subsystems
- Each lane gets isolated git worktree
- Define explicit dependencies between lanes

**Reviewer tuning**
- Make `acceptance_criteria` objective and verifiable
- Provide examples of "done" in task description
- Use `out_of_scope` to prevent creep

**State management**
- Use `--resume` to continue interrupted work
- `loop status --tree` to visualize dependencies

## Troubleshooting

**Backend not found**
- Ensure backend CLI is in PATH (`codex`, `claude`, or `opencode`)
- Or set `--dispatch-backend native` for local module execution

**Timeouts**
- Increase `--dispatch-timeout` or `--artifact-timeout`
- Check `.loop/logs/` for slow phases

**Worker/reviewer not responding**
- Use `loop health` to check heartbeats
- Add `--require-heartbeat` to fail fast on stalls

**State stuck**
- Inspect `.loop/state.json`
- Use `--reset` to clear stale bus files

**Permission errors**
- `.loop/` must be writable
- Git worktree operations need repo write access

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

### CI

GitHub Actions workflow [`loop-ci.yml`](.github/workflows/loop-ci.yml) runs on `push` to `main`/`master` and on `pull_request`:

- `uv sync --frozen --group dev`
- `uv run --group dev --with pytest-cov pytest --cov=src/loop_kit --cov-report=xml`
- `uv run --group dev pytest -m integration` when integration-marked tests exist
- `uv run --group dev ruff check src/loop_kit tests`
- `uv run --group dev --with mypy mypy src/loop_kit` (optional, non-blocking)

The workflow uploads `coverage.xml` to Codecov and stores JUnit XML test results as workflow artifacts.

## License

MIT
