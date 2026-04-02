# loop-kit

PM-driven review-loop orchestrator for AI coding agents.

loop-kit runs a multi-round review loop: a **Worker** writes code, a **Reviewer** checks it, and the loop repeats until approval or max rounds. It supports OpenAI Codex and Anthropic Claude as worker/reviewer backends, with automatic dispatch and real-time streaming output.

## Quick Start

```bash
# Install
pip install loop-kit
# or: uv add loop-kit

# Initialize loop directory in your project
loop init

# Write a task card (see .loop/examples/task_card.json)
# Then run the loop with auto-dispatch
loop run --task .loop/task_card.json --auto-dispatch --worker-backend codex --reviewer-backend codex
```

## Prerequisites

- Python >= 3.11
- Git repository (the orchestrator uses git commits as the source of truth)
- At least one AI backend installed: [codex](https://github.com/openai/codex) or [claude](https://docs.anthropic.com/en/docs/claude-code)

## CLI Reference

```
loop init                  Create .loop/ directory structure and templates
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
| `--auto-dispatch` | off | Automatically invoke worker/reviewer backends |
| `--dispatch-backend native` | native | Subprocess transport |
| `--worker-backend codex\|claude` | codex | Backend for worker dispatch |
| `--reviewer-backend codex\|claude` | codex | Backend for reviewer dispatch |
| `--dispatch-timeout N` | 600 | Per-dispatch timeout in seconds |
| `--dispatch-retries N` | 2 | Retries on non-zero dispatch exit |
| `--dispatch-retry-base-sec N` | 5 | Base backoff seconds between dispatch retries (max delay 60s) |
| `--artifact-timeout N` | 90 | Post-dispatch artifact wait in seconds |
| `--require-heartbeat` | off | Require live heartbeat while waiting |
| `--heartbeat-ttl N` | 30 | Heartbeat freshness threshold in seconds |
| `--single-round` | off | Run exactly one round and exit |
| `--round N` | - | Round number for single-round mode |
| `--resume` | off | Resume from .loop/state.json |
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

### Key JSON schemas

**task_card.json**
```json
{
  "task_id": "T-001",
  "goal": "One-sentence goal",
  "in_scope": ["file or module"],
  "out_of_scope": [],
  "acceptance_criteria": ["measurable criterion"],
  "constraints": []
}
```

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
     │  Worker  │ │ Reviewer │   (codex/claude subprocess)
     │(codex/   │ │(codex/   │
     │ claude)  │ │ claude)  │
     └──────────┘ └──────────┘
```

Each round runs as a **fresh subprocess** (`python -m loop_kit run --single-round`), so code changes in the orchestrator itself take effect immediately — the orchestrator can improve itself.

Backend discovery uses `shutil.which()` plus known install paths. The backend registry (`register_backend()`) supports adding custom backends.

## Prompt Templates

`loop init` creates templates in `.loop/templates/`:

- **worker_prompt.txt** — Worker prompt with `{task_id}`, `{round_num}`, `{agents_md}`, `{role_md}`, `{task_card_section}`, `{prior_context_section}`, `{work_report_path}` placeholders
- **reviewer_prompt.txt** — Reviewer prompt with `{task_id}`, `{round_num}`, `{role_md}`, `{review_report_path}` placeholders

The worker also reads `AGENTS.md` and `docs/roles/code-writer.md` from the project root. The reviewer reads `docs/roles/reviewer.md`.

## Development

```bash
# Clone and install editable
git clone <repo-url>
cd loop-kit
uv sync

# Run tests
uv run --group dev pytest

# Run as module
uv run python -m loop_kit init
```

## License

MIT
