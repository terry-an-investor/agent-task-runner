# Loop Kit — Let AI Write Code, Then Review Itself

[![ci](../../actions/workflows/loop-ci.yml/badge.svg)](../../actions/workflows/loop-ci.yml)

**You describe the task. AI writes the code. AI reviews the code. Repeat until it's right.**

Most AI coding tools stop after generating code. Loop Kit closes the loop — a **Worker AI** writes the implementation, a **Reviewer AI** checks it against your acceptance criteria, and the system iterates automatically until the code passes or you step in. No more copy-pasting AI output and reviewing it yourself.

## The Problem

AI coding assistants are great at writing code, but someone still has to:

- Review the output for correctness and quality
- Catch missed edge cases and style violations
- Iterate on feedback until it's actually done

That "someone" is usually **you** — which defeats the point of using AI in the first place.

## The Solution

```
You write a task card → AI writes → AI reviews → AI fixes → ... → ✅ Approved
```

Loop Kit orchestrates the entire cycle:

| What | Before | With Loop Kit |
|------|--------|---------------|
| Writing code | Manual or AI-assisted | Worker AI, scoped to your task |
| Reviewing | You, or a teammate | Reviewer AI, against your criteria |
| Iterating | Back-and-forth PR comments | Automatic, until approved or max rounds |
| Tracking | Scattered across PRs & chats | Structured state, full audit trail |

## ✨ Why Teams Use Loop Kit

- **Ship faster** — Eliminate the manual review bottleneck between AI-generated code and your codebase
- **Consistent quality** — Reviewer AI enforces your standards every time, no fatigue, no shortcuts
- **Full audit trail** — Every round, every decision, every diff is logged. Know exactly what changed and why
- **Works with your tools** — Plug in Codex, Claude, or OpenCode. Git-native, no new workflows to learn
- **Scales with complexity** — Multi-lane execution for large changes, dependency-aware task graphs
- **Zero lock-in** — Open source, extensible backend registry, your data stays in your repo

## Quick Start

```bash
# 1. Initialize loop directory in your project
loop init

# 2. (optional) Build offline module map for faster context
loop index

# 3. Create a task card, then run
loop run --task .loop/task_card.json --auto-dispatch --worker-backend codex --reviewer-backend codex
```

**What you need:**

- Python >= 3.11
- Git repository
- At least one AI backend: [codex](https://github.com/openai/codex), [claude](https://docs.anthropic.com/en/docs/claude-code), or [opencode](https://opencode.ai)

**The only thing you write is the task card:**

```json
{
  "task_id": "T-001",
  "goal": "Add input validation to user registration endpoint",
  "in_scope": ["src/api/auth.py", "tests/test_auth.py"],
  "out_of_scope": ["UI changes"],
  "acceptance_criteria": [
    "Email format validated",
    "Password strength enforced",
    "Tests cover edge cases"
  ]
}
```

That's it. Loop Kit handles the rest — writing, reviewing, iterating — until the code meets your criteria.

## How It Works

### The Loop

```
You define the task → AI writes → AI reviews → AI fixes → ... → ✅ Approved
```

Each round:

1. **Worker AI** reads your task card and writes code
2. **Reviewer AI** checks the output against your acceptance criteria
3. If changes are needed, the loop repeats automatically (up to `--max-rounds`)
4. When approved, you get a clean diff and full audit trail

```
Round 1: Worker → Reviewer → changes_required
Round 2: Worker → Reviewer → approve ✅
```

### Stay in the Loop

Everything is tracked in `.loop/`:

| File | What it tells you |
|------|-------------------|
| `state.json` | Current round, status, decisions |
| `logs/feed.jsonl` | Full event log for debugging |
| `archive/{task_id}/` | Artifacts from every round |

Run `loop status --tree` anytime to see where things stand.

## Deep Dive

<details>
<summary><strong>Architecture & File Bus Protocol</strong></summary>

### How Components Talk

All communication happens through JSON files in `.loop/`:

```
PM → Worker:   task_card.json / fix_list.json
Worker → PM:   work_report.json
PM → Reviewer: review_request.json
Reviewer → PM: review_report.json
```

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

### Artifact Formats

**task_card.json** — Your instructions to the system:

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

**review_report.json** — The reviewer's verdict:

```json
{
  "task_id": "T-001",
  "round": 1,
  "decision": "approve|changes_required",
  "blocking_issues": [{"severity": "high", "file": "src/main.py", "reason": "..."}],
  "non_blocking_suggestions": ["..."]
}
```

`state.json` is the single source of truth between rounds.

</details>

<details>
<summary><strong>Core Concepts</strong></summary>

### Knowledge System

Loop Kit doesn't just send raw code to the AI — it retrieves **relevant context**:

- **Facts** — Project-specific conventions (`project_facts.md`)
- **Pitfalls** — Known issues to avoid (`pitfalls.md`)
- **Patterns** — High-confidence coding patterns (`patterns.jsonl`)
- **Module Map** — Offline codebase index (`module_map.json`)

Context is scored by relevance, keeping prompts focused and costs down.

### State Machine

| State | Meaning |
|-------|---------|
| `idle` | No active contract |
| `awaiting_work` | Worker phase |
| `awaiting_review` | Reviewer phase |
| `done` | Terminal (approved, timeout, or blocked) |

### Session Management

- **Quickstart**: Fresh context for cold starts (round 1)
- **Handoff**: Structured bridge every round for both roles
- **Warm resume**: Reuse backend sessions for low-latency continuation
- **Session rotation**: Set `--max-session-rounds` to intentionally rotate

</details>

<details>
<summary><strong>CLI Reference</strong></summary>

### Commands

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
| `--tree` | off | Render dependency tree from `task_card.json` |
| `--loop-dir PATH` | .loop | Loop bus directory |

### `loop dispatch-metrics` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--task-id ID` | all task IDs | Filter by task ID |
| `--role all\|worker\|reviewer` | all | Filter by role |
| `--loop-dir PATH` | .loop | Loop bus directory |

```bash
loop dispatch-metrics --task-id T-715 --role worker
```

### `loop diff` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--task-id ID` | **required** | Task ID archive key (e.g. `T-604`) |
| `--base-round N` | **required** | Base archive round number (`>=1`) |
| `--head-round N` | **required** | Head archive round number (`>=1`) |
| `--artifact all\|state\|work_report\|review_report` | all | Which artifact to diff |
| `--loop-dir PATH` | .loop | Loop bus directory |

### `loop report` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--task-id ID` | from `state.json` | Task ID to summarize |
| `--format json\|markdown` | json | Output format |
| `--loop-dir PATH` | .loop | Loop bus directory |

### `loop knowledge` subcommands

- `list [--category CAT]` — Print facts, pitfalls, patterns
- `add --pattern TEXT --category CAT --confidence 0..1 --source ORIGIN` — Append pattern
- `prune --older-than DAYS` — Remove stale entries
- `dedupe` — Remove duplicates

### Configuration

`loop run` reads defaults from `.loop/config.yaml` (preferred) or `.loop/config.json`.

Environment variable overrides:

- `LOOP_MAX_ROUNDS` → `RunConfig.max_rounds`
- `LOOP_DISPATCH_TIMEOUT` → `RunConfig.dispatch_timeout`
- `LOOP_BACKEND_PREFERENCE` → `RunConfig.backend_preference` (comma-separated)

Resolution order: `CLI args > env vars > config file > built-in defaults`

</details>

<details>
<summary><strong>Performance & Optimization</strong></summary>

### Metrics

`loop dispatch-metrics` reports phase latencies (`startup_ms`, `context_to_work_ms`, `work_to_artifact_ms`, `total_ms`) and work subphases (`read/search/edit/test/unknown`).

### Optimization Tips

| Symptom | Fix |
|---------|-----|
| Slow startup (`startup_ms` high) | Use `--max-session-rounds N`, pre-index with `loop index` |
| Slow work phase (`work_to_artifact_ms` high) | Narrow `in_scope`, sharpen `acceptance_criteria`, split into lanes |
| High retry count | Improve criteria clarity, add project facts, tune templates |

### Backend Choice

| Backend | Best for |
|---------|----------|
| `codex` | Fast, good for boilerplate |
| `claude` | Strong reasoning, complex refactors |
| `opencode` | Local, no API costs |

### Best Practices

- **Task cards**: One clear goal, specific acceptance criteria, use `lanes` for multi-module changes
- **Reviewer tuning**: Make criteria objective and verifiable, use `out_of_scope` to prevent creep
- **State management**: Use `--resume` to continue interrupted work, `loop status --tree` to visualize dependencies

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

| Problem | Fix |
|---------|-----|
| Backend not found | Ensure CLI is in PATH, or use `--dispatch-backend native` |
| Timeouts | Increase `--dispatch-timeout` or `--artifact-timeout`, check `.loop/logs/` |
| Worker/reviewer not responding | Use `loop health`, add `--require-heartbeat` |
| State stuck | Inspect `.loop/state.json`, use `--reset` to clear stale files |
| Permission errors | Ensure `.loop/` is writable, git worktree needs repo write access |

</details>

<details>
<summary><strong>Development</strong></summary>

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

</details>

## License

MIT
