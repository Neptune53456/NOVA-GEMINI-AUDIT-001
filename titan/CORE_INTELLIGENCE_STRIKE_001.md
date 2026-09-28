# NOVA Core Intelligence Strike 001

## Architecture discovered

Normal conversation follows `ConversationService -> AgentLoop -> ContextBuilder/ProjectBrain -> model_router-backed EngineAdapter -> CapabilityRegistry -> compact tool observation -> next model turn`. `AgentLoop` selects a bounded tool subset and applies confirmations for writes. An explicit goal follows `ConversationService -> InitialGoalPlanner -> GoalRunner -> CapabilityRegistry -> evidence/verification -> OutcomeLearner`. `ContextBuilder` supplies bounded memory, experience, project, and mission state. The model router is invoked by the planner and engine. Missions use a separate durable bounded path. The goal path uses `UncertaintyEngine`, `RecoveryPolicy`, optional `DeliberationEngine`, and bounded budgets.

The production `create_app` wired `InitialGoalPlanner` into `GoalRunner` for initial planning, but left its `planner` parameter unset. Thus the runner's recovery branch blocked after any failed step despite calculating uncertainty and recovery advice. The replan method was exercised only by explicitly injected test planners. The goal runner also replaced the current plan during recovery, letting the file readback criterion be treated as verified after an unrelated new plan.

## Baseline weaknesses and hypotheses

The capability harness in `tests/test_core_intelligence_strike_001.py` uses deterministic planner replies and the real goal runner, registry, filesystem capabilities, confirmations, and evidence store. It exercises simple, multistep, tool selection, failed-action recovery, unavailable capability, state across confirmation, repeated failure, and verification. The same eight probes ran on a separate detached worktree at `c3e0e72` and on the final candidate.

Hypotheses checked:

- Recovery was disconnected in the production composition: confirmed; failed read blocked before the second planner response.
- Repeated failed actions consumed budget without new evidence: confirmed; three identical reads occurred with an injected replanner.
- A changed plan could downgrade an exact readback criterion: confirmed; reading a different file yielded `completed_verified`.
- Basic bounded planning, ordered multistep execution, confirmation, and invalid capability rejection were already working: five baseline probes passed; retained those contracts.

## Modifications retained

- `GoalRunner` reuses `InitialGoalPlanner` for a bounded recovery plan when no dedicated replanner is injected. It gives the planner the failure category, target path, latest compact observation, original objective, and current bounded context. The existing registry validates the new plan.
- It rejects a recovery plan whose first action repeats the failed action with identical arguments, before executing it.
- It persists a backend-derived exact readback requirement in the goal checkpoint and checks that requirement against the verified current plan after replanning.
- It respects remaining model-call budget and fails closed on an invalid recovery plan.

No new planner, model routing policy, tool executor, or memory subsystem was introduced. General adaptive deliberation was not added because the measured failure came from the disconnected existing recovery path. No trading semantics or external runtime behavior was changed.

## Before/after results

| Signal | Base `c3e0e72` | Strike 001 |
|---|---:|---:|
| Eight deterministic capability probes passed | 5/8 | 8/8 |
| Failed read recovered with alternative plan | blocked | verified after one replan |
| Identical failed read executions | 3 | 1 |
| Unrelated file read declared exact target verified | yes | no (`completed_unverified`) |
| Simple direct objective | 1 planner call, 1 tool | 1 planner call, 1 tool |

These are controlled behavioral tests, not a general intelligence score. No paid provider or production service was used.

## Self-attack and remaining weaknesses

The probes include an unavailable capability, an unverifiable result, a repeated failure, a plan that changes the verified target, and a write that must cross confirmation before readback. The backend regression tests exercise durable goals, missions, risk, memory, and conversation behavior. A model can still choose a valid but semantically poor alternative plan; the backend cannot generally prove that an arbitrary natural-language objective was achieved. The exact-readback guard covers the backend-derived file criterion, not every custom success criterion. Recovery cannot automatically carry readback expectations across a plan that omits a matching verifier. There is no independent live-provider quality measurement.

Recommended next strike: improve semantic goal completion by adding narrow, backend-owned verification profiles to additional capabilities and testing them on real task traces without weakening confirmations or budgets.

## Files changed and validation

- `nova_api/autonomy.py`: recovery connection, anti-repeat check, persistent exact criterion.
- `tests/test_autonomy.py`: existing anti-spin expectation updated to assert early stop.
- `tests/test_core_intelligence_strike_001.py`: eight capability probes.
- `titan/CORE_INTELLIGENCE_STRIKE_001.md`: this report.

Validation with the local existing virtual environment and `--no-cov --confcutdir=tests`: baseline harness 5 passed, 3 failed; candidate harness and focused regression 83 passed; broader relevant backend regression 144 passed (25 deprecation warnings). `--confcutdir=tests` was needed because the base commit's root `conftest.py` imports absent, untracked `smart_memory.py`. No dependency was installed or copied into the isolated worktree.
