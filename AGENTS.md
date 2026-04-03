# AGENTS.md

## Workflow

- **Trivial tasks** (typos, one-line fixes): do them directly
- **Non-trivial tasks**: act as the **PM/orchestrator**. Create a task card and run `loop run --auto-dispatch` to delegate to the worker backend. **Do not write the code yourself.**

## Source Structure

- All code in `src/loop_kit/orchestrator.py` — do not split into modules.
- Wrappers: `cli.py` (re-exports main), `__main__.py` (`python -m loop_kit`), `__init__.py` (version).
- Tests in `tests/test_orchestrator.py`.

## Coding Constraints

- Python 3.11+ (use `X | Y` union syntax, not `Union`).
- Internal functions are `_`-prefixed — do not rename them.
- All file I/O: `Path` objects, UTF-8 encoding.
- JSON output: `ensure_ascii=False, indent=2`.
- State contract: `state.json` is the single source of truth between outer and inner processes.
- Backend registry: use `register_backend()` to add backends, do not modify dispatch directly.
- Windows: prompts piped via stdin (8191 char CLI limit), use `os.name == "nt"` for platform checks.
- File locking: `_LoopLock` uses `msvcrt` (Windows) or `fcntl` (Unix).

## Testing

- `uv run --group dev pytest` to run all tests.
- Use `tmp_path` fixture for filesystem tests.
- Mock `subprocess.run`/`subprocess.Popen` for dispatch tests.
- `_configure_loop_paths` mutates module globals — always pair with `monkeypatch.setattr`.

## Validation Commands

```bash
uv run python -m py_compile src/loop_kit/orchestrator.py
uv run --group dev pytest
```
