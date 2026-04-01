# HANDOFF.md

New session onboarding guide. Read this first, then `CLAUDE.md` and `AGENTS.md`.

## Current State

- **2 commits** on `master` branch, clean working tree
- **43 tests passing** (`uv run --group dev pytest`)
- **v0.2.0** in `__init__.py` and `pyproject.toml`
- All core features working: auto-dispatch, streaming, archive, resume, backend registry, file locking

## Origin

This package was extracted from `C:\Users\terryzzb\Desktop\wind-agent-excel\tools\orchestrator.py` after 10 iterations of improvement (T-604 through T-613). The original project uses loop-kit as an editable dependency (`uv add --editable ../git_repos/loop-kit`).

The extraction made 3 key changes from the original:
1. `ROOT = Path.cwd()` (was `Path(__file__).resolve().parent.parent`)
2. Self-command `sys.executable -m loop_kit` (was `uv run python tools/orchestrator.py`)
3. Error messages say `loop init` (was `uv run python tools/orchestrator.py init`)

## What Works

- `loop init` — creates `.loop/` dirs, example task card, prompt templates
- `loop run --auto-dispatch --worker-backend codex --reviewer-backend codex` — full automated loop
- `loop status`, `loop health`, `loop archive` — inspection commands
- `loop run --resume` — resume from interrupted state
- Backend registry: `register_backend()` for adding codex/claude/custom
- Streaming output: real-time codex event parsing during dispatch
- Subprocess-per-round: code changes take effect immediately
- Windows + Unix: file locking, stdin piping, path handling

## Immediate Next Steps (Roadmap)

These are candidate improvements, not commitments. Prioritize by impact.

### High Priority
1. **Port remaining 147 tests** — The original project has 190 tests; loop-kit has 43. The other 147 test edge cases for streaming, archive, resume, state contract, locking. Import path is the only difference (`from loop_kit import orchestrator`).
2. **End-to-end smoke test** — Write a test that actually runs `loop run --auto-dispatch` with a mock backend, validating the full multi-round cycle.
3. **Proper packaging** — Add `.gitignore`, LICENSE, CI config (GitHub Actions with pytest on 3.11/3.12).

### Medium Priority
4. **Retry/backoff on dispatch failure** — Currently fails immediately on non-zero exit. Add configurable retry with exponential backoff.
5. **Structured logging** — Replace `print()` + file log with `logging` module. Feed log (`_feed_event`) should be the primary log, console output secondary.
6. **Config file** — Support `.loop/config.json` or `pyproject.toml [tool.loop-kit]` for default backend, timeout, max-rounds so CLI flags aren't needed every time.
7. **Cancellation signal** — Allow graceful shutdown (SIGINT/Ctrl+C) mid-round: write partial state, don't leave stale lock files.

### Lower Priority
8. **Template inheritance** — Allow projects to override only parts of the prompt template (e.g., custom worker instructions without rewriting the whole template).
9. **Metrics/telemetry hook** — Optional callback/interface for reporting round duration, tokens, approval rates.
10. **Plugin system** — Allow projects to register custom backends, custom role prompts, pre/post-round hooks.

## Known Gotchas

- **`.loop/lock` stale file**: After a crash/interrupt, `.loop/lock` may persist. Delete it manually (`rm .loop/lock`) before re-running. The lock uses OS-level file locking, so a dead process doesn't hold it, but the file itself remains.
- **Windows CLI length**: Prompts are piped via stdin because Windows has ~8191 char CLI limit. All backends must support stdin piping.
- **Test import**: Tests use `from loop_kit import orchestrator`, not `from tools import orchestrator`.
- **Global state**: `_configure_loop_paths()` mutates module-level globals (`LOOP_DIR`, etc.). Tests that call it should use `tmp_path` and restore defaults after.

## First Thing To Do

```bash
cd C:\Users\terryzzb\Desktop\git_repos\loop-kit
uv sync
uv run --group dev pytest        # verify 43 tests pass
uv run python -m loop_kit init   # smoke test
```

Then read `src/loop_kit/orchestrator.py` to understand the full codebase (~2200 lines, single file).
