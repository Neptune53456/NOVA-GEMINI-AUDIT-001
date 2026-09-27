# V1 Hardening — Phase 4 real-world validation

Date: 2026-09-17  
Branch: `feature/nova-interface-foundation`

## Outcome

Phase 4 executed 18 bounded scenarios through Nova production services and isolated persistent stores. Result: **16 PASS, 1 PARTIAL, 1 FAIL, 0 critical failures**. The release-readiness recommendation is **NO-GO for the final benchmark** until the real Windows UIA stale-reference defect is reproduced with diagnostic evidence and fixed.

The campaign used temporary filesystem workspaces only. The UIA scenario launched a disposable Notepad process, entered unsaved test text, and terminated that exact process. No personal file, external account, credential, destructive Git action, or irreversible operation was used.

## Results

| Area | Scenarios | Outcome |
|---|---|---|
| Conversation/provider | REAL-01, 02, 13, 16 | 4 PASS |
| Memory/context | REAL-03 | PASS |
| GoalRunner and confirmation | REAL-04–09, 18 | 7 PASS |
| Restart/recovery | REAL-10, 11 | 2 PASS |
| Filesystem/rollback | REAL-12 | PASS |
| Windows UIA | REAL-14, 15 | 1 FAIL, 1 PARTIAL |
| Endurance/isolation | REAL-17 | PASS |

There were no mutations without confirmation, no writes outside the temporary workspace, no accepted expired confirmation, no replayed completed mutation, no cross-conversation action leak, no false `completed_verified`, and no rollback corruption.

## Real provider behavior

REAL-01 used `ConversationService` with `NovaEngineAdapter` once and completed with a non-empty response in about 4.2 seconds. Authoritative token usage was unavailable. The bounded fallback test for REAL-02 passed with two attempts and one fallback; Phase 3's live runtime observation remains the supporting real outage evidence (Omniroute timeout followed by a successful configured fallback). REAL-16 verified that total provider unavailability terminates with the safe `provider_unavailable` category.

## Restart, confirmation, and rollback

Persistent memory survived reopening and was retrievable. Running mission state restarted as paused with `interrupted_uncertain`; completed goal steps were preserved and not replayed. A refused confirmation caused no write. An expired token caused no write and is now surfaced as `GoalStateError("invalid_confirmation")` instead of an internal `ValueError`. Failed exact verification rolled back the reversible temporary write.

## UIA findings

Notepad discovery and UI inspection succeeded (59–61 elements observed), but immediate `computer.ui.set_value` failed twice with `STALE_ELEMENT_REFERENCE`. The target remained the disposable Notepad window and no wrong-window action was observed. A candidate ordering fix passed its synthetic regression but did not change the real failure, so it was removed rather than retained speculatively. REAL-14 is FAIL and REAL-15 is PARTIAL because safe stale-reference rejection works but recovery by re-observation was not demonstrated.

## Endurance

Ten sequential goals shared one isolated production stack. All ten reached `completed_verified`; 13 actions were observed (3 mutating, 10 discovery), with 3 requested and approved confirmations. First latency was 80.84 ms, last latency 84.77 ms, median 87.78 ms. No duplicate action, state leak, replay, stuck state, or journal inconsistency was detected. This was deliberately a bounded sequence rather than a soak test.

## Product defects and fixes

One systemic correction was retained:

- Invalid or expired GoalRunner confirmation tokens are translated to the public `GoalStateError("invalid_confirmation")` contract before capability execution. A regression proves rejection and absence of mutation.

Two clusters remain open:

- Real Notepad UIA references become stale between inspection and action; root cause is not yet established.
- A successful standalone conversation left the SQLite journal handle open long enough for Windows temporary-directory cleanup to report `WinError 32`; product behavior succeeded, but fixture/process cleanup needs an explicit lifecycle investigation.

## Validation

- Targeted backend campaign checks: 7 passed, then 3 passed for the confirmation fix, then 4 passed for provider/isolation behavior, then 2 passed for single-use confirmation and restart memory.
- UIA synthetic candidate tests: 2 passed, but the candidate was removed after the real rerun still failed.
- Frontend state tests: 2 passed.
- Frontend lint: passed.
- Frontend production build: passed.
- Full backend suite: **not run**. The user-defined gate requires systemic fixes to be complete, and the UIA cluster remains open.

## Remaining V1 risks and recommendation

The blocking V1 risk is semantic UIA reliability on a real Windows application. Before the final benchmark, capture bounded generation/signature evidence around `inspect_ui` and `_resolve_element`, establish why a stable Notepad element is invalidated, add a failing regression matching the native behavior, and rerun only REAL-14/15. Investigate explicit journal close/lifecycle separately. If both clusters are resolved and targeted regressions remain green, run the full backend suite once, then the final benchmark.
