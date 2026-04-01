# HANDOFF.md

New session onboarding guide. Read this first, then `CLAUDE.md` and `AGENTS.md`.

## Current State

- **10+ commits** on `master` branch, clean working tree
- **139 tests passing** (`uv run --group dev pytest`)
- **v0.2.0** in `__init__.py` and `pyproject.toml`
- All core features working: auto-dispatch, streaming, archive, resume, backend registry, file locking, retry/backoff
- CI configured: GitHub Actions with pytest on Python 3.11/3.12/3.13, ubuntu + windows

## Origin

This package was extracted from `wind-agent-excel/tools/orchestrator.py` after 10 iterations of improvement (T-604 through T-613). The original project uses loop-kit as an editable dependency (`uv add --editable ../git_repos/loop-kit`).

The extraction made 3 key changes from the original:
1. `ROOT = Path.cwd()` (was `Path(__file__).resolve().parent.parent`)
2. Self-command `sys.executable -m loop_kit` (was `uv run python tools/orchestrator.py`)
3. Error messages say `loop init` (was `uv run python tools/orchestrator.py init`)

## What Works

- `loop init` — creates `.loop/` dirs, example task card, prompt templates
- `loop run --auto-dispatch` — full automated loop with codex/claude backends
- `loop status`, `loop health`, `loop archive` — inspection commands
- `loop run --resume` — resume from interrupted state via state.json contract
- Backend registry: `register_backend()` for adding codex/claude/custom
- Streaming output: friendly summaries in non-verbose mode, raw JSON in verbose mode
- Subprocess-per-round: code changes take effect immediately (self-bootstrapping)
- Retry/backoff: configurable via `--dispatch-retries` and `--dispatch-retry-base-sec`
- Function index: worker prompt includes dynamic def/class line index
- Windows + Unix: file locking, stdin piping, path handling

## Completed Work (since extraction)

### Phase 1: Infrastructure ✅
- `.gitignore`, `LICENSE` (MIT), CI (`.github/workflows/ci.yml`)

### Phase 2: Test Coverage ✅
- Expanded from 43 to 139 tests covering: pure utilities, streaming/parsing, dispatch, locking, heartbeat, state contract, CLI commands, validation, archive, backend registry

### Phase 3: Robustness & Readability ✅ (core items)
- Dispatch retry with exponential backoff (`--dispatch-retries`, `--dispatch-retry-base-sec`)
- Dynamic function index in worker prompt (reduces codex code-reading rounds)
- SIGINT handling (`_terminate_subprocess_on_interrupt`)
- AGENTS.md rewritten as concise coding constraint checklist
- Role docs: `docs/roles/code-writer.md`, `docs/roles/reviewer.md`
- Stream summary simplification: removed ~170 lines of unreliable shell command string parsing, now uses only structured JSON fields

## Remaining Improvements (nice-to-have)

| Task | Priority | Notes |
|------|----------|-------|
| Outer loop graceful shutdown | Medium | `_run_multi_round_via_subprocess` SIGINT propagation to single-round subprocess |
| Duplicate dirty tree warning | Low | Single-round subprocess redundantly checks (parent already checked) |
| Structured logging | Low | Replace `print()` + file log with `logging` module |
| Config file | Low | `.loop/config.json` or `pyproject.toml [tool.loop-kit]` for defaults |
| End-to-end smoke test | Low | Full multi-round cycle test with mock backend |
| Template inheritance | Low | Allow overriding parts of prompt templates |
| Metrics/telemetry hook | Low | Optional callback for round duration, tokens, approval rates |

## Known Gotchas

- **`.loop/lock` stale file**: After a crash/interrupt, `.loop/lock` may persist. Delete it manually (`rm .loop/lock`) before re-running. The lock uses OS-level file locking, so a dead process doesn't hold it, but the file itself remains.
- **Windows CLI length**: Prompts are piped via stdin because Windows has ~8191 char CLI limit. All backends must support stdin piping.
- **Test import**: Tests use `from loop_kit import orchestrator`, not `from tools import orchestrator`.
- **Global state**: `_configure_loop_paths()` mutates module-level globals (`LOOP_DIR`, etc.). Tests that call it should use `tmp_path` and restore defaults after.
- **Worker role**: Claude Code is the **PM/orchestrator**, not the worker. Non-trivial coding tasks should use `loop run --auto-dispatch`.

## First Thing To Do

```bash
cd C:\Users\terryzzb\Desktop\git_repos\loop-kit
uv sync
uv run --group dev pytest        # verify 139 tests pass
uv run python -m loop_kit init   # smoke test
```

Then read `src/loop_kit/orchestrator.py` to understand the full codebase (~2100 lines, single file).
