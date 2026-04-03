# Loop Kit — Let AI Write Code, Then Review Itself

[![ci](../../actions/workflows/loop-ci.yml/badge.svg)](../../actions/workflows/loop-ci.yml)

**You describe the task. AI writes the code. AI reviews the code. Repeat until it's right.**

Most AI coding tools stop after generating code. Loop Kit closes the loop — a Worker AI writes the implementation, a Reviewer AI checks it against your acceptance criteria, and the system iterates automatically until the code passes or you step in.

## The Problem

AI coding assistants are great at writing code, but someone still has to review the output, catch edge cases, and iterate on feedback. That "someone" is usually **you** — which defeats the point.

## The Solution

```
You write a task card → AI writes → AI reviews → AI fixes → ... → ✅ Approved
```

| What | Before | With Loop Kit |
|------|--------|---------------|
| Writing | Manual or AI-assisted | Worker AI, scoped to your task |
| Reviewing | You or a teammate | Reviewer AI, against your criteria |
| Iterating | Back-and-forth PR comments | Automatic, until approved or max rounds |
| Tracking | Scattered across PRs & chats | Structured state, full audit trail |

## ✨ Why Teams Use Loop Kit

- **Ship faster** — Eliminate the manual review bottleneck
- **Consistent quality** — Your standards enforced every time, no fatigue
- **Full audit trail** — Every round, every decision, every diff logged
- **Works with your tools** — Codex, Claude, or OpenCode. Git-native
- **Scales with complexity** — Multi-lane execution, dependency-aware task graphs
- **Zero lock-in** — Open source, extensible backend registry, your data stays in your repo

## Quick Start

```bash
# 1. Initialize
loop init

# 2. (optional) Pre-index for faster context
loop index

# 3. Create a task card, then run
loop run --task .loop/task_card.json --auto-dispatch --worker-backend codex --reviewer-backend codex
```

**Prerequisites:** Python >= 3.11, Git repo, at least one AI backend ([codex](https://github.com/openai/codex), [claude](https://docs.anthropic.com/en/docs/claude-code), or [opencode](https://opencode.ai)).

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

That's it. Loop Kit handles the rest. Run `loop status --tree` anytime to see where things stand.

## Deep Dive

<details>
<summary><strong>How It Works</strong></summary>

Each round:

1. **Worker AI** reads the task card and writes code
2. **Reviewer AI** checks the output against acceptance criteria
3. If changes are needed, the loop repeats (up to `--max-rounds`)
4. When approved, you get a clean diff and full audit trail

```
Round 1: Worker → Reviewer → changes_required
Round 2: Worker → Reviewer → approve ✅
```

All state is tracked in `.loop/`:

| File | What it tells you |
|------|-------------------|
| `state.json` | Current round, status, decisions |
| `logs/feed.jsonl` | Full event log |
| `archive/{task_id}/` | Artifacts from every round |

</details>

<details>
<summary><strong>Architecture</strong></summary>

### Components

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

### File Bus Protocol

```
PM → Worker:   task_card.json / fix_list.json
Worker → PM:   work_report.json
PM → Reviewer: review_request.json
Reviewer → PM: review_report.json
```

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

### Knowledge System

Loop Kit retrieves **relevant context** rather than injecting raw code:

- **Facts** — Project conventions | **Pitfalls** — Known issues
- **Patterns** — Coding patterns | **Module Map** — Offline codebase index

</details>

<details>
<summary><strong>CLI Reference</strong></summary>

### Commands

```
loop init                  Create .loop/ directory and templates
loop index                 Build offline module map
loop run                   Run the full review loop
loop knowledge             Manage built-in knowledge
loop status                Show current state (--tree for DAG view)
loop health                Show worker/reviewer heartbeat
loop dispatch-metrics      Summarize latency metrics
loop diff                  Compare artifacts between rounds
loop report                Summarize task progress
```

### `loop run` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--task PATH` | `.loop/task_card.json` | Task card path |
| `--max-rounds N` | 3 | Max review rounds |
| `--auto-dispatch` | off | Auto-invoke backends each round |
| `--worker-backend` | codex | `codex`, `claude`, or `opencode` |
| `--reviewer-backend` | codex | `codex`, `claude`, or `opencode` |
| `--dispatch-timeout N` | 0 | Per-dispatch timeout (0=unlimited) |
| `--dispatch-retries N` | 2 | Retries on non-zero exit |
| `--max-session-rounds N` | 0 | Session reuse before rotation |
| `--resume` | off | Resume from state.json |
| `--verbose` | off | Stream backend stdout |

Full flag list: `loop run --help`

### Configuration

`loop run` reads from `.loop/config.yaml` (preferred) or `.loop/config.json`.

Env var overrides: `LOOP_MAX_ROUNDS`, `LOOP_DISPATCH_TIMEOUT`, `LOOP_BACKEND_PREFERENCE`.

Resolution order: `CLI args > env vars > config file > built-in defaults`

</details>

<details>
<summary><strong>Performance & Optimization</strong></summary>

### Metrics

`loop dispatch-metrics` reports phase latencies (`startup_ms`, `context_to_work_ms`, `work_to_artifact_ms`, `total_ms`) and work subphases (`read/search/edit/test/unknown`).

### Optimization

| Symptom | Fix |
|---------|-----|
| Slow startup | `--max-session-rounds N`, pre-index with `loop index` |
| Slow work phase | Narrow `in_scope`, sharpen `acceptance_criteria`, split into lanes |
| High retry count | Improve criteria clarity, add project facts, tune templates |

### Backend Choice

| Backend | Best for |
|---------|----------|
| `codex` | Fast, good for boilerplate |
| `claude` | Strong reasoning, complex refactors |
| `opencode` | Local, no API costs |

</details>

<details>
<summary><strong>Troubleshooting</strong></summary>

| Problem | Fix |
|---------|-----|
| Backend not found | Ensure CLI is in PATH, or use `--dispatch-backend native` |
| Timeouts | Increase `--dispatch-timeout` or `--artifact-timeout` |
| Not responding | Use `loop health`, add `--require-heartbeat` |
| State stuck | Inspect `state.json`, use `--reset` |
| Permission errors | Ensure `.loop/` is writable |

</details>

<details>
<summary><strong>Development</strong></summary>

```bash
git clone <repo-url> && cd <repo-dir> && uv sync
uv run --group dev pytest
uv run python -m loop_kit init
```

CI runs on push/PR: tests, coverage, ruff, optional mypy.

</details>

## License

MIT
