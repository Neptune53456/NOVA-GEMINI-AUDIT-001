# Nova 1.1 — Execution Kernel convergence

## Why this exists

Nova 1.0 has two useful orchestrators with overlapping lifecycle responsibilities: `GoalRunner` and `MissionManager`. Nova 1.1 deliberately does **not** merge them in one risky rewrite. Phase 2 extracts the restart/idempotency primitives they must share first.

## Current responsibilities

`GoalRunner` owns model-derived bounded goals, independent verification evidence, replanning, goal budgets, durable goal state and outcome memory.

`MissionManager` owns small pre-validated mission plans, compact per-step checkpoints, progress events and conversational mission execution.

Both need the same safety semantics for mutation identity, restart recovery, confirmation binding and no-replay guarantees.

## Shared kernel introduced in Phase 2

`nova_api/execution_kernel.py` now owns deterministic primitives used by both runtimes:

- stable mutation identity derived from owner + step + capability + effect fingerprint;
- structural mutation entries (`NOT_STARTED`, `STARTED_UNCERTAIN`, `COMPLETED`, `VERIFIED`, `ROLLED_BACK`);
- non-replaying reconciliation of uncertain filesystem writes through the durable transaction journal.

The kernel does **not** decide goals, plans, permissions or success. Those remain with the orchestrators and Trusted Control Plane.

## Duplicated concepts still present

- current step / completed steps;
- pending/running/paused/terminal lifecycle;
- confirmation request handling;
- restart checkpoint state;
- capability execution and compact results;
- event journal correlation.

## Intentionally different concepts

- Goal-specific evidence and `completed_verified` semantics;
- goal replanning and model budgets;
- mission progress events and pre-validated plans;
- outcome-memory writing.

These should not be flattened merely to make the APIs look alike.

## Proposed future interface

A future `ExecutionKernel` may own only the deterministic execution lifecycle:

1. `prepare_step(owner, step)`
2. `request_confirmation(owner, step)`
3. `begin_mutation(owner, step)`
4. `execute_capability(step)`
5. `record_observation(result)`
6. `reconcile_after_restart(owner, step)`
7. `finalize_step(owner, verified_result)`

`GoalRunner` would remain responsible for planning/replanning and evidence criteria. `MissionManager` would remain responsible for mission decomposition/progress until a later migration proves that a single owner model is cleaner.

## Migration order

1. Shared mutation identity/recovery primitives — **done in Phase 2**.
2. Shared durable confirmation record semantics — goal + mission paths now use the same store contract.
3. Shared step checkpoint envelope.
4. Long-duration restart benchmark.
5. Only then consider converging the outer state machines.

## Compatibility risks

A direct merge today could regress SSE event names, confirmation UX, goal evidence semantics, restart behavior and legacy mission APIs. The incremental kernel approach keeps the V1 contracts intact while making duplication measurable and removable.
