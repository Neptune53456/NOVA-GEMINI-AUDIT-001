# TITAN Known Issues

As of 2026-09 - TITAN Campaign 001

## VERIFIED BUGS - OPEN

### BUG-001: _auto_paper ignores entry_analysis.action

- Severity: HIGH
- File: service.py:486-522
- Method: _auto_paper execution block
- Evidence: tests/test_entry_confirmed_fix.py Case 1
- Status: FIXED — Campaign 002
- Fix applied: Added `if opportunity.get("entry_analysis",{}).get("action")!="ENTER": continue` gate after QUALIFIED check.
- Result: _auto_paper now fail-closed; only executes when entry_analysis.action=="ENTER".

### BUG-002: Tautological entry_confirmed=True assignment

- Severity: MEDIUM
- File: service.py:975-976
- Status: FIXED — lines removed in Campaign 002
- Evidence: EXP-003 (Campaign 001) + EXP-011 (rejected)

entry_confirmed=True was set for every QUALIFIED opportunity. QUALIFIED already means passed all hard gates.
Setting entry_confirmed=True for every QUALIFIED added no filter — it was redundant.
FIX: Lines 975-976 deleted. No replacement producer added.
ENTRY IS NOW FAIL-CLOSED: without an evidence-backed confirmation mechanism, QUALIFIED -> action=WAIT -> zero new-entry fills. This is the authoritative fail-closed state.

### BUG-003: No production mechanism produces entry_confirmed=True

- Severity: LOW — UNRESOLVED ARCHITECTURAL DEBT
- Subsystem: All of production
- Status: UNRESOLVED — no evidence-backed confirmation producer exists or was implemented

After removing BUG-002 (tautological fix), NO production mechanism sets entry_confirmed=True.
This means ALL QUALIFIED opportunities will have entry_analysis.action=WAIT indefinitely.
_auto_paper will fire zero new-entry fills until a real confirmation mechanism is designed and implemented.
This is ACCEPTABLE for the foundation campaign — fail-closed is the correct posture.
A future "Entry Confirmation Engine" (design proposal only, no repo evidence) may address this properly.
Any such mechanism MUST be grounded in repository evidence before implementation.
Do NOT implement time-based or scheduler-cycle confirmation without evidence-backed duration.

### BUG-003: No production mechanism produces entry_confirmed=True

- Severity: LOW
- Subsystem: All of production
- Status: OPEN

No API, lifecycle, or manual process sets entry_confirmed. Only service.py:975-976 sets it.

## DESIGN DEBT

### AD-001: Redundant entry_confirmed/status dimensions

status==QUALIFIED and entry_confirmed==True both mean ready in current implementation.
Either remove entry_confirmed or give it independent meaning.

### AD-002: No schema validation on opportunity persistence

storage.py:282 save_star_opportunity accepts any dict without type checking.

## RESOLVED

### RES-001: Baseline reproduction - entry_confirmed absence causes WAIT

Verified: no entry_confirmed -> action=WAIT -> _auto_paper fires fills (BUG-001).
Reference: EXP-009 in EXPERIMENT_JOURNAL.md

### RES-002: entry_analysis is a gate, not advisory

Confirmed: WAIT reason is entry_confirmation_pending, IGNORE is redundant with REJECTED.
Reference: EXP-006 in EXPERIMENT_JOURNAL.md

### RES-003: position_action is not an entry timing gate

position_action() manages existing positions (HOLD/ADD/REDUCE/EXIT), not entry timing.
Reference: EXP-007 in EXPERIMENT_JOURNAL.md

### RES-004: test_god_eye_closure_gate.py needs update after BUG-001 fix

It relies on no-gate behavior and will need entry_confirmed=True or entry_analysis set after fix.
Reference: EXP-008 in EXPERIMENT_JOURNAL.md