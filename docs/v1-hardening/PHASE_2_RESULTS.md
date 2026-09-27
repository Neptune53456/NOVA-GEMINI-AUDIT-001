# V1 Hardening — Phase 2 controlled campaign

Date: 2026-09-17  
Branch: `feature/nova-interface-foundation`

## Campaign and selection

The frozen campaign contains 40 scenarios: all 24 official Phase 1 scenarios, 8 cases from `conversations_test_pc_assistant(1)(1).jsonl`, 8 cases from `projet_ia_test_pack_1000(1)(1).jsonl`, and 0 cases from `train.jsonl`.

The legacy sample was stratified for ambiguity/context, multi-turn state, PC/filesystem actions, multi-step ordering, destructive confirmation, unavailable attachments, impossible requests, change-of-mind, colloquial language, safe path resolution, and no-fake-success. Near-duplicates were avoided. The train corpus was excluded because its sampled function families are CRM, ecommerce, finance, calendar, DevOps, and data analysis; none has an exact Nova-owned semantic equivalent. Four exact mappings are frozen in the manifest; no fuzzy matching is allowed.

Tier counts are A: 13, B: 10, C: 2, and D: 15. All executable A/B checks ran before any consideration of real execution. No destructive real action and no real provider call was made. Filesystem mutations used pytest temporary workspaces.

## Scorecard

| Status | Count |
|---|---:|
| PASS | 17 |
| FAIL | 0 |
| PARTIAL | 3 |
| BLOCKED_EXPECTED | 4 |
| BLOCKED_EXTERNAL | 0 |
| NOT_EXECUTABLE | 16 |
| Total | 40 |

Strict pass rate is **42.5% (17/40)**. Actionable success rate is **87.5% (21/24)**, where the numerator is PASS plus correctly BLOCKED_EXPECTED and the denominator excludes BLOCKED_EXTERNAL and NOT_EXECUTABLE. Verified success across the entire frozen campaign is **42.5% (17/40)**; among behaviorally evaluable cases it is **70.8% (17/24)**. These rates describe this campaign only and do not establish production readiness.

By tier: A = 10 PASS, 1 PARTIAL, 2 BLOCKED_EXPECTED; B = 7 PASS, 1 PARTIAL, 2 BLOCKED_EXPECTED; C = 2 NOT_EXECUTABLE; D = 1 PARTIAL and 14 NOT_EXECUTABLE.

The strongest measured areas were filesystem/Git (3/3 PASS), memory/context component contracts (3/3 PASS), mission persistence/restart (3/3 PASS), mocked UIA behavior (2/2 PASS), bounded goal failure behavior, rollback, and cancellation. Confirmation refusal and expiration, absence of a structured planner, and absence of a vision provider all stopped safely as expected.

No critical failure was observed. No scenario was BLOCKED_EXTERNAL because external providers were deliberately not invoked. This does not prove their availability.

## Non-PASS clustering

`P2-HARNESS-01` affects 18 scenarios: all 16 imported legacy cases plus CONV-02 and GOAL-01. The proven cause is missing scenario-level orchestration: the current recorder can summarize a completed goal, but no campaign runner currently drives arbitrary multi-turn conversations and binds their journal/action evidence to legacy criteria. CONV-02 lacks the complete distinct-concurrent-conversation exercise, and GOAL-01 lacks the complete repeated-resubmission/file-effect exercise. Confidence is high. Phase 3 should add the isolated runner before changing product behavior.

`P2-UI-01` affects UI-01. Backend state projection completed successfully, but the conversation card and presence panel were not observed by an automated frontend test. The missing frontend regression harness is proven and confidence is high. Phase 3 should add a focused UI state-projection test.

There are no observed product-failure clusters in this run. The absence of FAIL results must be read alongside the 16 NOT_EXECUTABLE cases; it is not evidence that the legacy behaviors pass.

## Performance and provider usage

Latency was available for 23 of 24 evaluable scenarios: median **110 ms**, nearest-rank p95 **280 ms**. The largest observed outlier was FS-03 at **380 ms**. These are local/mock test timings, not user-facing end-to-end latency, and the sample is small.

Where counters were available, median model calls were 0 and median actions were 0 because many safety/state checks require no action. Fallback, replan, and confirmation frequencies over the 24 evaluable scenarios were respectively **1/24 (4.2%)**, **1/24 (4.2%)**, and **5/24 (20.8%)**. The sole fallback was fully mocked. Real model/provider attempts: **0**. Tokens in/out were unavailable. Provider-attempt fields are null where attribution could not be established rather than inferred.

## Harness limitations and Phase 3 priorities

The campaign reused targeted component tests as evidence; it did not yet create a full natural-language scenario executor. Consequently, ambiguity, change-of-mind, colloquial language, attachment absence, and legacy intent-to-tool behavior cannot be judged fairly. Frontend state agreement remains manual. Per-goal provider, token, rollback, and recovery attribution is incomplete in the underlying journal, so unavailable values remain null.

Recommended Phase 3 correction order:

1. Add an isolated multi-turn campaign runner with scripted/mock providers, temporary workspaces, journal capture, and deterministic evidence adapters; rerun the same frozen legacy IDs.
2. Complete the CONV-02 and GOAL-01 scenario drivers.
3. Add a focused frontend state synchronization regression harness for UI-01.
4. Add goal-correlated provider/fallback, rollback, recovery, restart, and token telemetry without recording prompts or sensitive content.
5. Only after deterministic coverage is in place, run a minimal bounded real-provider sample for behavior unavailable to mocks.

## Validation record

The targeted campaign command completed with **27 tests passed in 5.52 s** and two dependency deprecation warnings. The first attempt used the system Python and failed during collection because FastAPI was unavailable; it executed no scenario and was discarded. The successful run used the existing `.venv`; no dependency was installed. No full suite, frontend lint/build, destructive action, external account action, or real provider call was performed.

Artifacts: `phase2_campaign.json`, `phase2_results.jsonl`, `phase2_failure_clusters.json`, and this report. Benchmark-only code was extended with exact legacy mappings, deterministic evaluators, and scorecard aggregation; product behavior was not changed.
