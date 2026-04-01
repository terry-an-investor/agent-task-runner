# AGENTS.md

Technical architecture and constraints for loop-kit.

## Single-Source Module

Everything lives in `src/loop_kit/orchestrator.py`. This is intentional — the orchestrator is a single coherent process, and splitting it would add import complexity without real benefit.

The other files are thin wrappers:
- `cli.py` — re-exports `main()`
- `__main__.py` — enables `python -m loop_kit`
- `__init__.py` — version string

## Subprocess-Per-Round

The outer loop (`_run_multi_round_via_subprocess`) spawns a fresh Python subprocess for each round via `_single_round_subprocess_cmd()`. This means:

1. Code changes in `orchestrator.py` take effect on the next round
2. Each round starts with clean state — no leaked globals
3. The outer loop itself can be improved by the worker

The single-round subprocess re-enters `cmd_run()` with `--single-round --round N`, reads `state.json` for the contract, and writes updated state on completion.

## State Contract

`state.json` fields used between outer and inner:
- `task_id` (str) — must match task card
- `base_sha` (str) — git SHA at loop start
- `round` (int) — current round number
- `state` — one of: `idle`, `awaiting_work`, `awaiting_review`, `done`
- `outcome` — `approved`, `worker_timeout`, `reviewer_timeout`, error outcomes
- `round_details` — list of per-round summaries

## Backend Registry

```python
register_backend("codex", _build_codex_command, _resolve_codex_exe)
register_backend("claude", _build_claude_command, _resolve_claude_exe)
```

Each backend provides:
- `build_cmd_fn(exe, prompt) -> (cmd_list, session_id, stdin_text)`
- `resolve_exe_fn(backend) -> exe_path`

To add a new backend, call `register_backend()` with the two functions.

## Streaming

Auto-dispatch uses `Popen` with threaded I/O:
- `_collect_streamed_process_output()` — threads for stdin write, stdout read, stderr read
- `_stream_dispatch_stdout_line()` — parses codex JSON events for friendly summaries
- `--verbose` flag streams raw output instead

The outer loop's subprocess output is also streamed via `_collect_streamed_text_output()`.

## File Locking

`_LoopLock` uses OS-level byte-range locking:
- Windows: `msvcrt.locking()` with `LK_NBLCK` / `LK_UNLCK`
- Unix: `fcntl.flock()` with `LOCK_EX | LOCK_NB` / `LOCK_UN`

Single-round subprocesses skip lock acquisition (the parent holds it).

## Prompt Templates

Templates in `.loop/templates/` use Python `str.format()` with named placeholders. The worker template expects: `AGENTS.md` and `docs/roles/code-writer.md` to exist in the project root.

## Windows Compatibility

- CLI arg length: prompts are piped via stdin to avoid Windows ~8191 char limit
- Path handling: `os.path.normcase()` for case-insensitive comparison
- Executable resolution: checks `shutil.which()`, then known install paths (npm, .local/bin)
