# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

**loop-kit** is a PM-driven review-loop orchestrator for AI coding agents. It runs multi-round Worker/Reviewer cycles via file bus in `.loop/`, supporting codex and claude backends with auto-dispatch.

## Your Role

You are the **maintainer and developer** of loop-kit. You write code, fix bugs, add features, and ensure tests pass.

## Architecture

Single source file: `src/loop_kit/orchestrator.py` (~2200 lines). Entry points: `cli.py` and `__main__.py`.

### Key design patterns

- **Subprocess-per-round**: Each round runs as `python -m loop_kit run --single-round`, so code changes take effect immediately (self-bootstrapping)
- **File bus protocol**: All state via JSON files in `.loop/` (task_card, work_report, review_request, review_report, fix_list, state)
- **State contract**: `state.json` is the single source of truth between outer loop and inner single-round processes
- **Backend registry**: `register_backend(name, build_cmd_fn, resolve_exe_fn)` — extensible without modifying core
- **File locking**: `_LoopLock` using msvcrt (Windows) or fcntl (Unix), skipped for single-round subprocesses
- **Streaming**: `Popen` with threaded stdout/stderr reading, codex JSON event parsing for friendly output

## Development Commands

```bash
uv sync                                    # install dependencies
uv run --group dev pytest                  # run all tests
uv run --group dev pytest -x               # run tests, stop on first failure
uv run --group dev pytest -k test_name     # run specific test
uv run python -m loop_kit init             # smoke test
```

## Code Style

- Python 3.11+ (use `X | Y` union syntax, not `Union`)
- Functions prefixed with `_` are internal (module-private)
- Keep everything in `orchestrator.py` — do not split into multiple modules unless a compelling reason arises
- All file I/O uses `Path` objects and UTF-8 encoding
- JSON output uses `ensure_ascii=False, indent=2`

## Testing Conventions

- Tests in `tests/test_orchestrator.py`
- Use `tmp_path` fixture for filesystem tests
- Mock `subprocess.run`/`subprocess.Popen` for dispatch tests
- Test both Windows and Unix paths where relevant
- Keep tests focused and independent

## Key Constants

- `DEFAULT_MAX_ROUNDS = 3`
- `POLL_INTERVAL_SEC = 5`
- `DEFAULT_HEARTBEAT_TTL_SEC = 30`
- `DEFAULT_DISPATCH_TIMEOUT_SEC = 600`
- `DEFAULT_DISPATCH_ARTIFACT_TIMEOUT_SEC = 90`
- `DISPATCH_STREAM_POLL_SEC = 0.1`

## Document Hierarchy

When documents disagree: `src/loop_kit/orchestrator.py` > `AGENTS.md` > `CLAUDE.md` > `README.md`.

## Workflow

- For trivial tasks (typos, one-line fixes, obvious small changes), just do them directly — no task cards needed
- For non-trivial tasks, act as the **PM/orchestrator**, not the worker. Create a task card and run `loop run --auto-dispatch` to delegate coding to the worker backend (codex/claude). Do not write the code yourself.
- Always commit after completing a set of changes. Do not leave uncommitted work.

## Known Gotchas

- **`.loop/lock` stale file**: After a crash/interrupt, `.loop/lock` may persist. Delete it manually before re-running. The lock uses OS-level file locking so a dead process doesn't hold it, but the file itself remains.

## Commit Convention

Write clear, descriptive commit messages. No emoji prefix required.
