# TITAN Known Issues

As of 2026-09 - TITAN Campaign 001

## VERIFIED BUGS - OPEN

### BUG-001: _auto_paper ignores entry_analysis.action

- Severity: HIGH
- File: service.py:486-522
- Method: _auto_paper execution block
- Evidence: tests/test_entry_confirmed_fix.py Case 1
- Status: OPEN

_auto_paper gates on status==QUALIFIED and risk_approved but never checks entry_analysis.action.
Fix: add gate before execution block.
NOTE: This is the primary bug. Fix first.

### BUG-002: Tautological entry_confirmed=True assignment

- Severity: MEDIUM
- File: service.py:975-976
- Status: OPEN - revert intended but not applied

entry_confirmed=True is set for every QUALIFIED opportunity. QUALIFIED already means passed all hard gates.
Setting entry_confirmed=True for every QUALIFIED adds no filter - it is redundant.
WARNING: Revert alone will have unintended consequences - see campaign report.

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