# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

**agent-task-runner** is a PM-driven review-loop orchestrator for AI coding agents.
It runs multi-round Worker/Reviewer cycles via file bus in `.loop/`,
supporting codex, claude, and opencode backends with auto-dispatch.

## Architecture

Single source file: `src/loop_kit/orchestrator.py`. Entry points: `cli.py` and `__main__.py`.

### Key design patterns

- **Subprocess-per-round**: Each round runs as `python -m loop_kit run --single-round`,
  so code changes take effect immediately (self-bootstrapping)
- **File bus protocol**: All state via JSON files in `.loop/`
  (task_card, work_report, review_request, review_report, fix_list, state)
- **State contract**: `state.json` is the single source of truth between
  outer loop and inner single-round processes
- **Backend registry**: `register_backend(name, build_cmd_fn, resolve_exe_fn)`
  — extensible without modifying core
- **File locking**: `_LoopLock` using msvcrt (Windows) or fcntl (Unix),
  skipped for single-round subprocesses
- **Streaming**: `Popen` with threaded stdout/stderr reading,
  codex JSON event parsing for friendly output

## Development Commands

```bash
uv sync                                    # install dependencies
uv run --group dev pytest                  # run all tests
uv run --group dev pytest -x               # stop on first failure
uv run --group dev pytest -k test_name     # run specific test
uv run python -m loop_kit init             # smoke test
```

## Code Style

- Python 3.11+ (use `X | Y` union syntax, not `Union`)
- Functions prefixed with `_` are internal (module-private)
- Keep everything in `orchestrator.py` — do not split into multiple modules
- All file I/O uses `Path` objects and UTF-8 encoding
- JSON output uses `ensure_ascii=False, indent=2`

## Testing

Tests in `tests/test_orchestrator.py`. Use `tmp_path` for filesystem tests,
mock `subprocess` for dispatch tests. Keep tests focused and independent.

## Workflow

- **Trivial tasks** (typos, one-line fixes): do them directly
- **Non-trivial tasks**: act as the **PM/orchestrator**. Create a task card
  and run `loop run --auto-dispatch` to delegate to the worker backend.
  Do not write the code yourself.
- Commit after completing a set of changes. Write clear, descriptive messages. Any self-made changes must be committed — no silent uncommitted edits.

## Known Gotchas

- **`.loop/lock` stale file**: After a crash/interrupt, `.loop/lock` may persist.
  Delete it manually before re-running. The lock uses OS-level file locking
  so a dead process doesn't hold it, but the file itself remains.

## Document Hierarchy

When documents disagree: `orchestrator.py` > `AGENTS.md` > `CLAUDE.md` > `README.md`.
