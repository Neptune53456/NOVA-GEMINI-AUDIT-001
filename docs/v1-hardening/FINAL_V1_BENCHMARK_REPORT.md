# Nova V1 — Final Benchmark Report

## Executive result

**READY TO FREEZE NOVA 1.0**

The frozen final manifest contains **38 scenarios**. Final results are **24 PASS**, **14 BLOCKED_EXPECTED**, **0 FAIL**, **0 PARTIAL**, **0 BLOCKED_EXTERNAL**, and **0 NOT_EXECUTABLE**. No critical safety failure was observed.

- Strict pass rate: **24/38 = 63.16%**.
- Evaluable success rate: **38/38 = 100.00%** when correct safe blocking/confirmation/clarification is counted as the expected behavior.
- Verified completion rate: **20/20 = 100.00%** for scenarios requiring concrete verified completion.
- Safety rate: **25/25 = 100.00%** across scenarios carrying explicit safety invariants.

The strict pass rate is intentionally lower because 14 scenarios are designed to end in a safe block, clarification, refusal, confirmation gate, or bounded failure rather than a completed action.

## Composition

Execution modes: **MOCKED_INTEGRATION: 11**, **DETERMINISTIC: 15**, **REAL_LOCAL: 9**, **MANUAL_REAL: 3**.

### Category results

| Category | Total | PASS | BLOCKED_EXPECTED | Other | Evaluable success |
|---|---:|---:|---:|---:|---:|
| UIA | 4 | 3 | 1 | 0 | 100% |
| confirmations/safety | 4 | 1 | 3 | 0 | 100% |
| conversation/routing | 4 | 3 | 1 | 0 | 100% |
| endurance | 3 | 3 | 0 | 0 | 100% |
| error handling | 3 | 0 | 3 | 0 | 100% |
| filesystem/tools | 4 | 3 | 1 | 0 | 100% |
| goals/planning | 5 | 2 | 3 | 0 | 100% |
| memory/context | 4 | 4 | 0 | 0 | 100% |
| provider/fallback | 3 | 1 | 2 | 0 | 100% |
| restart/recovery | 4 | 4 | 0 | 0 | 100% |

## Release-gate evidence

- Backend release suite: **1767 passed, 0 failed, 1 skipped**.
- Coverage: **84.26%**, above the established 84% threshold.
- Frontend gate: **2 targeted state tests passed**, lint passed, production build passed.
- Final targeted benchmark campaign: **196 tests passed, 0 failed**. The first launch used the global Python without FastAPI and aborted at collection; it executed no scenarios. The configured repository virtual environment was then used successfully.
- Real Windows UIA: **REAL-14 PASS** and **REAL-15 PASS**. The stale reference survived a generation change, one bounded re-resolution occurred, the action remained bound to the same Notepad editor, and the isolated fixture cleanup was later validated with window-specific close behavior.
- Endurance: **10/10 goals completed_verified**; median **87.78 ms**, first **80.84 ms**, last **84.77 ms**, delta **3.93 ms**. No state leak, duplicate action, replay, stuck presence state, memory pollution, or journal inconsistency was observed.

## Safety

No benchmark scenario observed a critical failure. Explicitly tracked invariants remained false wherever exercised: mutation without confirmation, mutation outside the allowed workspace, invalid or expired confirmation acceptance, cross-conversation leakage, duplicate mutation, replay of completed mutation, false verified success, unsafe UIA targeting, and rollback corruption.

The benchmark therefore records **25/25 safety-relevant scenarios without an observed safety violation**. This is evidence for the exercised paths, not a claim that every possible desktop/application state is proven safe.

## Performance and efficiency

Telemetry availability is intentionally sparse because the benchmark refuses to invent metrics. The results file contains authoritative per-scenario fields only where the underlying journal/provider emitted them.

- Endurance median: **87.78 ms** across 10 local safe goals.
- Endurance actions: **13 total**, including **3 mutating** and **10 discovery** actions.
- Endurance confirmations: **3 requested / 3 approved**.
- Provider/fallback benchmark evidence records **3 provider attempts** and **1 fallback(s)** in the provider category where authoritative counters were available.
- Authoritative token totals are not consistently available and are therefore **not estimated**.
- A general p95 across all 38 scenarios would be misleading because comparable elapsed timing is not present for most final rows; no synthetic p95 is reported.

## Memory, restart and recovery

Persistent memory after reopen/restart, ProjectBrain context retrieval, decision/procedure retrieval, bounded context isolation, restart-safe goal recovery, paused-goal recovery, completed-step no-replay behavior, and transient-reference invalidation are represented in the final manifest and passed their corresponding evidence gates.

## UIA / desktop control

The earlier `STALE_ELEMENT_REFERENCE` defect was traced to rejection on a global observation-generation mismatch. The corrected design uses an opaque descriptor tied to the owning window and supports one bounded deterministic re-resolution. The final real Notepad fixture confirmed both direct UIA interaction and stale-reference recovery without crossing to another window.

Notepad-specific fixture files are test infrastructure. The production reference/re-resolution behavior is generic UI Automation infrastructure; compatibility can still vary across Windows applications and custom UI frameworks.

## Known V1 limitations

- Cloud visual-provider validation remains dependent on external provider availability and has historically been degraded/blocked even though local visual capture foundations are implemented.
- Provider routing is subject to quota, payment, timeout and cooldown conditions.
- Confirmation tokens remain process-local and expiry-sensitive by design where the current contract applies.
- Modern Windows application UIA trees vary; a successful Notepad fixture does not prove universal compatibility across every application.
- Authoritative token accounting is unavailable for some providers/paths.
- Frontend automated coverage remains narrower than backend coverage.
- Known non-blocking warnings remain, principally SQLite resource warnings plus FastAPI/Starlette deprecations and existing line-ending notices.
- One Windows directory-symlink test remains skipped in the release suite.
- Historical Phase 2/3 legacy cases that lacked frozen conversation payloads remain historical harness-data limitations; they are not counted as failures in this frozen 38-scenario final benchmark.

## Product defects found during final benchmark

**None newly identified.** The final benchmark did not change product code. Previously discovered hardening defects, including stale shared UI state, expired confirmation handling and UIA stale-reference recovery, were fixed and regression-tested before the final benchmark manifest was frozen.

## Final conclusion

All current release gates represented in the provided repository snapshot are satisfied: backend suite green, coverage threshold met, frontend release checks green, real UIA path validated, restart/recovery validated, endurance validated, no critical safety violation observed, and the final 38-scenario benchmark is internally consistent.

**READY TO FREEZE NOVA 1.0.**
