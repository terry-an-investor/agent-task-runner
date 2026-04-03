# 📋 Task Cards Index

This directory contains structured task cards for the loop-kit project, following the PM-driven review-loop orchestrator workflow.

## 🎯 Priority P0 (Immediate)

| ID | Title | Est. | Dependencies |
|----|-------|------|--------------|
| [T-701](T-701_task_card.json) | Add state.json schema versioning with migration support | 2h | - |
| [T-702](T-702_task_card.json) | Introduce LoopKitError hierarchy and unify internal exceptions | 3h | T-701 |
| [T-703](T-703_task_card.json) | Refactor global paths with LoopPaths dataclass | 6h | T-701 |

## 🔶 Priority P1 (High)

| ID | Title | Est. | Dependencies |
|----|-------|------|--------------|
| [T-704](T-704_task_card.json) | Improve knowledge governance: deduplication and versioning | 3h | T-701 |
| [T-705](T-705_task_card.json) | Add integration tests for full round-trip with real subprocesses | 5h | T-701 |
| [T-706](T-706_task_card.json) | Add concurrent execution safety tests for file locking | 3h | - |

## 🟡 Priority P2 (Medium)

| ID | Title | Est. | Dependencies |
|----|-------|------|--------------|
| [T-707](T-707_task_card.json) | Enhance configuration system with env vars and YAML support | 4h | T-703 |
| [T-708](T-708_task_card.json) | Type feed events with dataclass for consistency | 2h | T-701 |
| [T-709](T-709_task_card.json) | Consolidate session management logic | 4h | T-703 |
| [T-710](T-710_task_card.json) | Add 'loop knowledge' CLI subcommand for knowledge management | 6h | T-704 |

## 🟢 Priority P3 (Low)

| ID | Title | Dependencies |
|----|-------|--------------|
| [T-711](T-711_task_card.json) | Add GitHub Action for CI/CD integration | T-705 |

---

## 🧭 2026-04-03 Roadmap Batch (P1 + P2)

### P1 Quick Wins

| ID | Title | Est. | Dependencies |
|----|-------|------|--------------|
| [T-720](T-720_task_card.json) | Phase 1 quick wins: diff/report/config validation and atomic pattern writes | 1-2d | T-719 |

### P2 Architecture & Capability

| ID | Title | Est. | Dependencies |
|----|-------|------|--------------|
| [T-721](T-721_task_card.json) | Refactor main loop into a table-driven state machine | 2-3d | T-720 |
| [T-722](T-722_task_card.json) | Orchestrator modularization (policy-gated split plan and execution) | 1-3d | T-721 |
| [T-723](T-723_task_card.json) | Add task dependency DAG support and blocked-task visibility | 2-3d | T-720 |
| [T-724](T-724_task_card.json) | Keyword-based knowledge retrieval for prompt compaction | 1-2d | T-720 |

P3 items are intentionally not planned in this batch.

---

## 📊 Existing Tasks (Before This Batch)

| ID | Title | Status |
|----|-------|--------|
| T-601 | Reduce poll interval | ✅ Done |
| T-602 | Skip artifact repoll on no-change | ✅ Done |
| T-603 | Slim round 2 prompt | ✅ Done |
| T-604 | Fast-fail permanent errors | ✅ Done |
| T-605 | Prefetch git diff | ✅ Done |
| T-606 | Cache function index | ✅ Done |
| T-607 | Dedupe stream output | ✅ Done |
| T-608 | Hardening pre-release | ✅ Done |
| T-609 | Log rotation and status redact | ✅ Done |
| T-610 | Reduce orchestrator redundancy | ✅ Done |
| T-611 | Task packet pre-dispatch | ✅ Done |
| T-612 | Knowledge layer governance | ✅ Done |
| T-613 | Index command | ✅ Done |
| T-614 | Subprocess zombie fix | ✅ Done |
| T-615 | Coerce confidence bool fix | ✅ Done |
| T-616 | State file backup | ✅ Done |
| T-617 | Auto-dispatch heartbeat | ✅ Done |
| T-618 | Knowledge file cap | ✅ Done |
| T-623 | Error message enhancement | ✅ Done |
| T-624 | Prompt section list | ✅ Done |
| T-625 | TypedDict bus messages | ✅ Done |
| T-626 | Feed event types | ✅ Done |
| T-627 | Session warm (codex + claude) | ✅ Done |
| T-628 | Session warm (opencode) | 🔄 In Progress |

---

## 🚀 Execution Order (Recommended)

### Batch 1: Foundation (P0)
1. Run T-701 (schema versioning)
2. Run T-702 (exception hierarchy) - depends on T-701
3. Run T-703 (LoopPaths) - depends on T-701

### Batch 2: Testing & Knowledge (P1)
4. Run T-706 (lock tests) - independent, can run parallel
5. Run T-705 (integration tests) - depends on T-701
6. Run T-704 (knowledge governance) - depends on T-701

### Batch 3: DX & Polish (P2)
7. Run T-708 (feed event types) - depends on T-701
8. Run T-709 (session manager) - depends on T-703
9. Run T-707 (config system) - depends on T-703
10. Run T-710 (knowledge CLI) - depends on T-704

### Batch 4: CI/CD (P3)
11. Run T-711 (GitHub Action) - depends on T-705

---

## 📝 How to Use Task Cards

Each JSON file follows the task card schema defined in AGENTS.md. Task cards may include:
- optional `depends_on` (legacy `dependencies` is also accepted)
- optional `lanes` for future parallel ownership planning

Lane validation rules:
- `lane_id` must be unique across lanes
- lane `depends_on` can only reference existing lane IDs and cannot self-reference
- `owner_paths` must be repo-relative literal paths (no absolute path, no `..`, no glob)
- `owner_paths` cannot overlap across lanes

To work on a task:

```bash
# Read the task card
cat .loop/tasks/T-701_task_card.json

# Run loop with the task as context
uv run python -m loop_kit run --task .loop/tasks/T-701_task_card.json
```

Or copy the task card content to `.loop/task_card.json` to make it the active task.

Use `uv run python -m loop_kit status --tree` to inspect dependency relationships and current blocked reasons.

---

**Total Tasks**: 35 (11 new + 24 existing)
**Completed**: 24
**In Progress**: 1
**To Do**: 10
