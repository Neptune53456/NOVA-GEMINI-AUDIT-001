# V1 Hardening — Phase 3 harness completion

Date: 2026-09-17  
Branch: `feature/nova-interface-foundation`

## Outcome

Phase 3 added an isolated multi-turn benchmark runner, a FIFO scripted provider, journal-aware criteria evaluation, goal-correlated metric derivation, complete deterministic drivers for CONV-02 and GOAL-01, and a lightweight frontend state-projection test for UI-01. Product routing and safety behavior were not changed to influence benchmark results.

The three Phase 2 PARTIAL scenarios are now PASS. The 16 legacy scenarios remain NOT_EXECUTABLE because the versioned campaign contains only their identifiers/categories: their selected conversation turns and per-scenario deterministic criteria are absent. The runner is ready to execute those arrays when the frozen payloads are supplied, but inventing turns from scenario titles would not be a fair rerun.

## Harness architecture

`nova_api/benchmark_harness.py` contains:

- `MultiTurnConversationRunner`, which creates a fresh service and conversation for every scenario, seeds only prior user/assistant turns, and drives the final user turn through `ConversationService.start_generation()` and `stream()`;
- deterministic cleanup of the in-memory conversation after evidence collection;
- collection of assistant output, streamed events, conversation-correlated journal events, goal IDs and action IDs;
- `ScriptedProvider`, with explicit `chat`/`tools` capabilities, FIFO replies, immutable call traces, explicit exhaustion errors and no network path;
- support for plain replies, tool-call replies and deliberately malformed scripted messages through the normal AgentLoop normalization path.

`EventJournal.for_conversation()` provides a bounded, sanitized correlation query. Existing `for_goal()` evidence remains authoritative for goal metrics.

## Evaluators and metrics

The deterministic criteria evaluator can now derive actions and confirmation evidence from journal events when explicit evidence is absent. Coverage includes `action_required`, `no_fake_success`, `safe_path_required`, `context_resolution_safe`, `destructive_confirmation_required`, `no_action_without_target`, and `tool_function_expected`. Unknown mappings or unavailable evidence continue to return `null`, never success.

Goal metrics now derive provider attempts, fallbacks, model calls, rollback count, recovery count and authoritative token totals from events already correlated by `goal_id`. Token fields remain null when no authoritative usage exists. Events for another goal are excluded by test.

## Gap rerun and delta

| Status | Phase 2 | Phase 3 | Delta |
|---|---:|---:|---:|
| PASS | 17 | 20 | +3 |
| PARTIAL | 3 | 0 | -3 |
| BLOCKED_EXPECTED | 4 | 4 | 0 |
| FAIL | 0 | 0 | 0 |
| BLOCKED_EXTERNAL | 0 | 0 | 0 |
| NOT_EXECUTABLE | 16 | 16 | 0 |
| Total | 40 | 40 | 0 |

Strict pass rate is **50.0% (20/40)**. Actionable success rate is **100.0% (24/24)** when PASS plus BLOCKED_EXPECTED is divided by scenarios excluding BLOCKED_EXTERNAL and NOT_EXECUTABLE. Phase 2 was 87.5% (21/24) under the same formula. No legacy improvement is claimed.

Official rerun evidence:

- CONV-02: distinct runner executions receive unique conversation IDs, disjoint journal evidence and no retained sessions.
- GOAL-01: two identical confirmed file goals receive distinct goal/action IDs, each performs one bounded mutation, and exact final content is verified.
- UI-01: the pure state projection covers `awaiting_confirmation → running → verifying → completed_verified` and each of cancelled/failed/blocked; confirmation is absent after every non-awaiting state.

## Remaining NOT_EXECUTABLE scenarios

`LEGACY-PC-001` through `LEGACY-PC-008` and `LEGACY-1000-001` through `LEGACY-1000-008` all have the same exact blocker: `phase2_campaign.json` identifies the external source records, but `scenarios.json` does not contain their conversation arrays or criteria and the named source datasets are not present in the repository. Their prior status is preserved.

## Product defects

None established. The remaining gap is a benchmark-input completeness defect, not evidence that Nova product behavior passes or fails those scenarios.

## Small real-runtime sample

One safe natural-language conversation was attempted once through `NovaEngineAdapter`: request “Réponds uniquement par OK.” The Omniroute route timed out once, the configured fallback returned a non-empty response, and the conversation completed successfully. Result: PASS. Observed provider/model attempts: 2 (one Omniroute timeout and one successful fallback). Authoritative token usage was unavailable. No destructive action, file mutation or retry occurred.

The sample was intentionally limited to one scenario because deterministic legacy coverage is blocked by missing inputs and further provider calls would not resolve that harness-data gap.

## Validation

- Backend: `10 passed` in the targeted `tests/test_benchmark.py` run with `-q --no-cov`.
- Frontend state test: `2 passed` with Node's built-in test runner.
- Frontend build: passed (`tsc -b && vite build`).
- Frontend lint: passed (`oxlint`).
- Python compilation: passed for `nova_api/benchmark.py`, `nova_api/benchmark_harness.py`, and `nova_api/journal.py`.
- Full product suite: not run, as required.
- Real provider calls: 2 observed attempts in one scenario; no retry.
- Critical failures observed: 0.

## Recommended Phase 4 actions

1. Add the frozen selected legacy turns and criteria to a versioned, sanitized benchmark fixture, with hashes linked to `phase2_campaign.json`.
2. Execute only the 16 legacy IDs through the completed runner and classify from output plus journal/state evidence.
3. Add goal IDs to any rollback/recovery/provider events that still lack them; preserve null for usage data providers do not report.
4. Investigate the single Omniroute timeout independently as provider reliability evidence, without changing routing solely for benchmark scores.
