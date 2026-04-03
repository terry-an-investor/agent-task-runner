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

# Create task card (see .loop/examples/task_card.json)
# Run with auto-dispatch
loop run --task .loop/task_card.json --auto-dispatch --worker-backend codex --reviewer-backend codex
```

## 📋 Table of Contents

- [Core Concepts](#core-concepts)
- [CLI Reference](#cli-reference)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Development](#development)

## Prerequisites

- Python >= 3.11
- Git repository (the orchestrator uses git commits as the source of truth)
- At least one AI backend installed: [codex](https://github.com/openai/codex), [claude](https://docs.anthropic.com/en/docs/claude-code), or [opencode](https://opencode.ai)

## CI

GitHub Actions workflow [`loop-ci.yml`](.github/workflows/loop-ci.yml) runs on `push` to `main`/`master` and on `pull_request`.

- `uv sync --frozen --group dev`
- `uv run --group dev --with pytest-cov pytest --cov=src/loop_kit --cov-report=xml`
- `uv run --group dev pytest -m integration` when integration-marked tests exist
- `uv run --group dev ruff check src/loop_kit tests`
- `uv run --group dev --with mypy mypy src/loop_kit` (optional, non-blocking)

The workflow uploads `coverage.xml` to Codecov and stores JUnit XML test results as workflow artifacts.

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

### `loop knowledge` commands

| Command | Description |
|---------|-------------|
| `loop knowledge list [--category CAT]` | Print facts, pitfalls, and patterns from `src/loop_kit/defaults/*.jsonl` in a table |
| `loop knowledge add --pattern TEXT --category CAT --confidence 0..1 --source ORIGIN` | Append a pattern entry to `src/loop_kit/defaults/patterns.jsonl` |
| `loop knowledge prune --older-than DAYS` | Remove entries whose `source_version` timestamp is older than `DAYS` |
| `loop knowledge dedupe` | Deduplicate defaults knowledge entries and report removals |

All write operations are atomic (`temp file -> rename`) and modify `src/loop_kit/defaults/*.jsonl` in place.

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

Validation rules:

- `max_rounds` must be `>= 1`
- `timeout`, `dispatch_timeout`, `dispatch_retries`, `dispatch_retry_base_sec`, `max_session_rounds`, `artifact_timeout`, and `heartbeat_ttl` must be `>= 0`
- Invalid CLI/env/config values fail fast with a validation error (`exit code 3`)
- JSON payloads loaded from bus/state/config/task files are capped at 5 MiB and rejected when oversized

### Archive

Round artifacts are archived to `.loop/archive/{task_id}/r{N}_{name}.json` using payload identity (`task_id`, `round`) before overwrite, preserving deterministic round history and rejecting cross-task writes.

## Performance Metrics

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
`lanes` is optional. Each lane requires `lane_id` and non-empty `owner_paths` (literal repo-relative paths).
`lane_id` must match `^[A-Za-z0-9][A-Za-z0-9_-]*$`.
When lanes are provided, the orchestrator validates:
- `lane_id` values are unique
- `depends_on` only references existing lane IDs
- lanes do not depend on themselves
- lane dependencies are acyclic (`lanes.depends_on` must form a DAG)
- `owner_paths` rejects absolute paths and traversal segments
- `owner_paths` has no overlap across lanes (same path or parent/child ownership)

When lanes are present and valid, the orchestrator computes deterministic execution stages for troubleshooting and future parallel scheduling:
- each stage contains lanes whose dependencies are already satisfied
- lane order is deterministic for equal-priority lanes (task-card declaration order)
- round logs include `Lane stage <index>: <lane set>`
- feed emits `lane_plan_stage` events with `stage_index`, `stage_count`, and `lanes`

`status` is system-managed while the loop runs: it is written to `in_progress` at start, `done` on approved completion, and `blocked` on non-approved terminal failures, including unsatisfied task dependencies before dispatch.

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

### Archive

Round artifacts are archived to `.loop/archive/{task_id}/r{N}_{name}.json` using payload identity (`task_id`, `round`) before overwrite, preserving deterministic round history.

## Performance Metrics

`loop dispatch-metrics` analyzes `.loop/logs/feed.jsonl` to report:

- **Phase latencies**: `startup_ms`, `context_to_work_ms`, `work_to_artifact_ms`, `total_ms`
- **Work subphases**: `read/search/edit/test/unknown` (counts & durations)

Interpretation:
- `startup_ms`: process startup + first output
- `context_to_work_ms`: initial prompt processing before execution
- `work_to_artifact_ms`: actual code work duration

### Core run-loop state machine

The orchestrator run loop uses explicit state metadata and trigger-driven transitions for the core states:

- `idle` (no active contract)
- `awaiting_work` (worker phase)
- `awaiting_review` (reviewer phase)
- `done` (terminal: approved, timeout, or blocked/error outcomes)

State names in `state.json` are unchanged for backward compatibility.

| Trigger | Transition | Kind | Notes |
|---------|------------|------|-------|
| `bootstrap` | `idle|done|awaiting_* -> awaiting_work` | normal | Initializes round contract for a task. |
| `prepare_round` | `awaiting_*|idle|done -> awaiting_work` | normal | Normalizes resumed/single-round execution start. |
| `worker_completed` | `awaiting_work -> awaiting_review` | normal | Worker artifact validated and review request prepared. |
| `reviewer_approved` | `awaiting_review -> done` | normal | Approved terminal path (`outcome=approved`). |
| `reviewer_changes_required` | `awaiting_review -> awaiting_work` | retry | Declarative retry transition (`round += 1`). |
| `worker_timeout` | `awaiting_work -> done` | timeout | Declarative timeout terminal path. |
| `reviewer_timeout` | `awaiting_review -> done` | timeout | Declarative timeout terminal path. |
| `terminal_error` | `any core state -> done` | error | Centralized blocked/error terminal transition. |
| `max_rounds_exhausted` | `awaiting_work|awaiting_review -> done` | retry | Terminal stop when max rounds reached. |

`state_transition` feed events are emitted through one centralized transition path and include `trigger` plus `transition_kind`.

Dispatch logs (`.loop/logs/*_dispatch.log`) redact common sensitive values before persistence (for example bearer tokens, API keys, and password-like fields).

### Archive

Round artifacts are archived to `.loop/archive/{task_id}/r{N}_{name}.json` using payload identity (`task_id`, `round`) before overwrite, preserving deterministic round history and rejecting cross-task writes.

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

Policy decision (T-722): `src/loop_kit/orchestrator.py` remains a single file. A physical module split is currently blocked because core runtime behavior still depends on shared module globals and targeted monkeypatch patterns in tests.

The single-file architecture is governed by explicit section boundaries in `_SECTION_OWNERSHIP_MAP`:

- `exceptions`
- `paths`
- `state`
- `lock`
- `dispatch`
- `config`
- `prompts`

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
