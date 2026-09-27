# Nova 1.1 — Phase 2 report

## Goal

Move from two independent restart models toward one deterministic execution substrate, then exercise it under repeated restart-safe work.

## Implemented

### Shared Execution Kernel

`nova_api/execution_kernel.py` is now used by both `GoalRunner` and `MissionManager` for mutation identity and uncertain-write reconciliation. It intentionally contains no LLM logic and grants no permissions.

### Mission durability

`MissionManager` now:

- assigns stable step IDs;
- creates durable confirmation records while keeping HMAC tokens process-local;
- reissues confirmation after restart rather than reviving the old token;
- binds an approval to the exact effect fingerprint;
- checkpoints mutating steps as `STARTED_UNCERTAIN` before execution;
- re-observes a crash-window filesystem write and advances only when the durable transaction proves the expected effect;
- safely re-runs interrupted read-only observation steps;
- records a recovery generation so repeated restart handling remains inspectable.

### GoalRunner convergence

GoalRunner uses the same `ExecutionKernel` mutation/reconciliation primitives without changing its verification-first completion contract.

### Conversation continuity

Conversation state can reconstruct bounded recent history plus active goal/mission associations after restart. In-flight process state is intentionally downgraded to idle rather than pretending a dead generation is still running.

### Endurance development test

A deterministic 40-goal restart-heavy test repeatedly reconstructs GoalRunner against the same durable stores and verifies that every read goal ends `completed_verified` with unique IDs and no terminal-state loss.

This is a development endurance check, **not** a claim of multi-hour real-world endurance. A Windows multi-hour benchmark remains a post-phase validation item.

## Deferred intentionally

- full GoalRunner/MissionManager state-machine merge;
- UIA + vision fusion;
- Memory V2 embeddings;
- Deliberation Engine;
- arbitrary OS rollback;
- distributed/multi-user persistence.
