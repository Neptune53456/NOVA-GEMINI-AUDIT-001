# NOVA Forge — Architecture State

**As of:** 2026-09 — TITAN Campaign 001
**Purpose:** Authoritative architectural understanding for future Claude Code sessions.
**Read by:** Any model resuming work on god_eye / star_finder / trader

## Authoritative Components and Data Flows

### Star Finder Pipeline (production entry path)

```
GodEyeScheduler
  └── run_star_finder()
        ├── run_scanner()
        ├── analyze_market()
        ├── _ensure_forecast()
        ├── build_opportunity()
        └── opportunity construction (direct path):
              ├── expected_net_return()
              ├── _event_fusion_for()
              ├── rejection_gates()           -> status = QUALIFIED or REJECTED
              ├── entry_timing()              -> entry_analysis.action = ENTER/WAIT/IGNORE
              ├── star_score()
              ├── store.save_star_opportunity()
              ├── lifecycle_transition()
              └── _auto_paper()               -> EXECUTION (FIXED - entry_analysis.action gate enforced)
```

### _auto_paper Execution Gate

Location: service.py:486-522

**POST-FIX gate logic (Campaign 002):**
  if status != QUALIFIED: continue
  if entry_analysis.action != "ENTER": continue   # FIXED — fail-closed
  if risk_approved is False or kill_switch: continue
  # execute fill

### entry_timing Signal Path

Location: star_finder.py:98-109

  if rejection_reasons: action = "IGNORE"
  elif not entry_confirmed: action = "WAIT"   (entry_confirmed defaults False)
  else: action = "ENTER"

Three action states:
- IGNORE: rejection_reasons present (redundant with REJECTED status)
- WAIT: QUALIFIED but entry_confirmed absent/false -- timing not confirmed
- ENTER: QUALIFIED AND entry_confirmed=True -- ready for execution

### Position Re-evaluation (separate from entry)

Uses position_action() (trader.py:303) -- thesis management for EXISTING positions.
Does NOT use entry_timing(). Separate system with no shared logic.
Actions: HOLD, ADD, REDUCE, EXIT

## Important Contracts and Invariants

- Lifecycle: DETECTED -> QUALIFIED/REJECTED -> ENTER -> OPEN -> HOLD/ADD/REDUCE/EXIT -> CLOSED -> RESOLVED
- Paper-only invariant: ALL trader operations are paper_only=True. No live market execution.
- run_star_finder is fully automated scheduler task (300s interval). No human confirmation.
- _auto_paper only fires when trader.config.mode == AUTO_PAPER (service.py:487)

## Known Architectural Ambiguities

- entry_confirmed redundancy: status==QUALIFIED and entry_confirmed==True both mean "ready"
  Future models must decide: remove entry_confirmed, or give it independent meaning
- entry_timing action=IGNORE redundancy: identical to status==REJECTED
- No schema validation on save_star_opportunity: any dict accepted, no type checking

## Decisions Future Models Must Preserve

1. run_star_finder is sole producer of star opportunities. Fixing BUG-001 requires modifying _auto_paper.
2. _auto_paper is the only execution path in AUTO_PAPER mode. No fallback.
3. entry_timing() is pure -- no side effects. Fix to entry_confirmed must call entry_timing() AFTER setting it.
4. test_entry_confirmed_fix.py is the specification test. test_god_eye_closure_gate.py must be updated to match.
5. Paper-only invariant is absolute. All changes are safe in isolation.

## Key File Locations

- Entry timing gate: star_finder.py:98-109 (entry_timing)
- Rejection gates: star_finder.py:51-76 (rejection_gates)
- _auto_paper (BUGGY): service.py:486-522
- run_star_finder: service.py:933-999
- Position re-evaluation: service.py:542-584 (reevaluate_paper_positions)
- position_action: trader.py:303-313
- Opportunity persistence: storage.py:282-295
- API exposure: api.py:256-262
- Specification test: tests/test_entry_confirmed_fix.py
- Closure gate test: tests/test_god_eye_closure_gate.py

Update this file when architectural decisions change.