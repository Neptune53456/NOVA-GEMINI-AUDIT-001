# TITAN ASTRA Backlog

As of 2026-09 - TITAN Campaign 001

Substantial improvements that justify a future large campaign.

---

## ASTRA-001: Fix Entry-Timing Gate Enforcement

Problem: _auto_paper fires fills without checking entry_analysis.action.
Why it matters: The designed safety gate is bypassed. WAIT opportunities still trigger fills.
Affected subsystem: nova_api/god_eye/service.py - _auto_paper method

Proposed scope:
- Add entry_analysis.action check before execution block in _auto_paper
- Run targeted tests: pytest tests/test_entry_confirmed_fix.py -v
- Update test_god_eye_closure_gate.py (relies on no-gate behavior)
- Verify all star_finder tests pass

Dependencies: None
Risks: LOW - single condition check. test_god_eye_closure_gate.py needs update.
Expected engineering benefit: _auto_paper decisions match designed behavior.
No profitability claim without validated evidence.

---

## ASTRA-002: Simplify Opportunity State Machine

Problem: status==QUALIFIED and entry_confirmed==True are redundant. BUG-002 must be removed or made meaningful.
Why it matters: Redundant dimensions create confusion about what gates entry.
Affected subsystem: service.py (run_star_finder), star_finder.py (entry_timing), storage.py

Proposed scope:
- After ASTRA-001: decide entry_confirmed semantics
  Option A: Remove entry_confirmed; gate on entry_analysis.action only
  Option B: Keep entry_confirmed as explicit signal from run_star_finder (remove tautology)
  Option C: Implement real secondary process (delayed confirmation, human review, external signal)
- Update all consumers if semantics change
- Update tests and API docs

Dependencies: ASTRA-001 (must be done first)
Risks: MEDIUM - changing/removing entry_confirmed affects API consumers and stored data
Expected engineering benefit: Cleaner state machine, reduced accidental coupling.
No profitability claim - pure architectural cleanup.

---

## ASTRA-003: Schema Validation for Opportunity Persistence

Problem: storage.py:282 save_star_opportunity accepts any dict without type checking.
Why it matters: Bad opportunities silently persisted, no early detection of data pipeline errors.
Affected subsystem: nova_api/god_eye/storage.py

Proposed scope:
- Define dataclass or TypedDict for star opportunity fields
- Add validation in save_star_opportunity
- Run existing tests to verify no regressions

Dependencies: None
Risks: LOW - only rejects malformed data
Expected engineering benefit: Catch errors early, better debuggability.

---

## ASTRA-004: Add Persistent Regression Test for entry_analysis Gate

Problem: No persistent regression test that verifies _auto_paper respects entry_analysis.action after fix.
Why it matters: Without regression test, gate could be removed again without failing tests.
Affected subsystem: tests/

Proposed scope:
- After ASTRA-001: refactor test_auto_paper_respects_entry_timing_action from baseline-reproduction to regression test
- Assert: _auto_paper skips fills when action=WAIT
- Assert: _auto_paper fills when action=ENTER
- Verify test passes post ASTRA-001 fix

Dependencies: ASTRA-001
Risks: LOW
Expected engineering benefit: Regression protection for entry-timing gate.

---

## Unprioritized Candidates

- Delimited confirmation process: Implement real separate step that sets entry_confirmed with independent meaning
- Unified decision framework: entry_timing() and position_action() are separate; consider unification
- Historical pattern analysis for timing: entry_timing does not use historical data about optimal entry timing

Each milestone requires: problem, why it matters, affected subsystem, proposed scope, dependencies, risks, expected engineering benefit. No profitability claims without validated evidence.