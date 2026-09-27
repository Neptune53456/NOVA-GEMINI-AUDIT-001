# TITAN Experiment Journal

All significant hypotheses and experiments are recorded here. Each entry should be searchable by future models to avoid repeating work.

---

## Experiment Log

---

### EXP-001: entry_analysis is ignored by _auto_paper

**Hypothesis:** `entry_analysis.action` (produced by `entry_timing()`) is intended to gate `_auto_paper` execution, but `_auto_paper` does not check it before firing fills.

**Evidence gathered:**
- `entry_timing()` (`star_finder.py:98-109`) returns `{action: "ENTER"|"WAIT"|"IGNORE"}`
- `run_star_finder` calls `entry_timing()` at `service.py:977`, stores as `opportunity["entry_analysis"]`
- `_auto_paper` at `service.py:486-522` gates only on `status == "QUALIFIED"` and `risk_approved`
- No reference to `entry_analysis` in `_auto_paper` execution block (lines 498-521)

**Test/Reproduction:** `tests/test_entry_confirmed_fix.py::test_auto_paper_respects_entry_timing_action` — Case 1

**Result:** CONFIRMED — `_auto_paper` fires fills for opportunity with `entry_analysis.action == "WAIT"`. Bug is real.

**KEEP**

**Files affected:** `nova_api/god_eye/service.py`

**Remaining uncertainty:** None — bug is definitively reproduced by the targeted test.

---

### EXP-002: entry_confirmed is never set in production

**Hypothesis:** `entry_confirmed` is a field in the opportunity schema but is never set by any production mechanism (no lifecycle transition, no API endpoint, no decision logic, no manual process).

**Evidence gathered:**
- Searched entire codebase: only setters are test code and the tautological fix at `service.py:975-976`
- `lifecycle_transition()` (`star_finder.py:138`) — only validates state transitions, never sets `entry_confirmed`
- `save_star_opportunity()` (`storage.py:282`) — persists any dict, no production logic sets `entry_confirmed`
- API layer (`api.py`) — no endpoint sets `entry_confirmed`
- Decision store — no decision type sets `entry_confirmed`

**Result:** CONFIRMED — `entry_confirmed` has no production producer.

**KEEP**

**Files affected:** All of production

**Remaining uncertainty:** None.

---

### EXP-003: The "fix" at service.py:975-976 is tautological

**Hypothesis:** Setting `entry_confirmed = True` for every `status == "QUALIFIED"` opportunity adds no additional filter because QUALIFIED already implies passing all hard gates (via `rejection_gates`).

**Evidence gathered:**
- `QUALIFIED` is set at `service.py:973` only when `rejection_reasons` is empty
- `rejection_gates()` (`star_finder.py:51-76`) implements all hard gates
- `entry_timing()` at `star_finder.py:101`: `elif not opportunity.get("entry_confirmed", False): action = "WAIT"`
- So `entry_confirmed=True` simply duplicates the QUALIFIED condition
- `_auto_paper` itself never checks `entry_confirmed` (it checks `entry_analysis.action` instead)

**Result:** CONFIRMED — The assignment `entry_confirmed=True` is derived directly from `status=QUALIFIED`, making it redundant. This is BUG-002.

**KEEP**

**Files affected:** `nova_api/god_eye/service.py:975-976`

**Remaining uncertainty:** None.

---

### EXP-004: Revert of lines 975-976 was completed before interruption

**Hypothesis:** The previous session completed the revert before being interrupted.

**Evidence:** Read `service.py:975-976` directly — both lines are confirmed present.

**Result:** REJECTED — The revert was NOT applied.

**Files affected:** None

**Remaining uncertainty:** None.

---

### EXP-005: entry_confirmed should be set by a separate confirmation process

**Hypothesis:** `entry_confirmed` is intended to represent a separate human review step, delayed confirmation, or market timing validation — not just a flag derived from QUALIFIED status.

**Evidence gathered:**
- `test_run_star_finder_sets_entry_confirmed` in `test_entry_confirmed_fix.py` explicitly describes the "fix" as `run_star_finder` setting `entry_confirmed=True` for QUALIFIED opportunities
- No separate confirmation step exists in `run_star_finder` beyond the QUALIFIED gate
- No API endpoint allows a user or external system to set `entry_confirmed`
- `run_star_finder` is a fully automated scheduler task (`scheduler.py` extra_tasks)

**Result:** REJECTED — No separate process exists or is indicated by the codebase. The intended design is that `run_star_finder` sets `entry_confirmed` for QUALIFIED opportunities (even if that assignment is currently tautological).

**Files affected:** None

**Remaining uncertainty:** The semantic intent of `entry_confirmed` beyond QUALIFIED — the test description suggests it should be set by `run_star_finder`, but this is identical to QUALIFIED in current implementation.

---

### EXP-006: entry_analysis is advisory only (not a gate)

**Hypothesis:** `entry_analysis` is informational metadata provided to the API/UI, not an execution gate. The actual entry decision is made by `_auto_paper` based solely on `status` and `risk_approved`.

**Evidence gathered:**
- `entry_timing()` returns IGNORE (when rejection_reasons non-empty), WAIT (when `entry_confirmed=False`), and ENTER
- IGNORE state is redundant with REJECTED status
- WAIT state reason is `["entry_confirmation_pending"]` — explicitly a gating message
- The test `test_auto_paper_respects_entry_timing_action` documents the bug as "entry_analysis.action is never checked"
- `get_star_opportunity` (`api.py:256`) and `get_ranked_opportunities` (`api.py`) expose `entry_analysis` to API consumers

**Result:** REJECTED — `entry_analysis.action` is designed as an execution gate. The WAIT state with `entry_confirmation_pending` reason is specifically designed to explain why entry is gated. Advisory data would not need a separate IGNORE state.

**Files affected:** None

**Remaining uncertainty:** None.

---

### EXP-007: position_action() in trader.py is a separate entry gate

**Hypothesis:** `position_action()` (used by `reevaluate_paper_positions`) is a separate entry gate that correctly implements the timing logic, and `_auto_paper` intentionally does not use `entry_analysis` because `position_action` handles it for re-evaluation.

**Evidence gathered:**
- `position_action()` (`trader.py:303-313`) does NOT call `entry_timing()`
- `position_action()` gates on: risk, thesis_invalidated, expected_net_return vs action_cost, uncertainty > 0.6, new_independent_evidence
- `position_action()` has no reference to `entry_confirmed` or `entry_analysis`
- `reevaluate_paper_positions()` calls `position_action()` for existing positions (HOLD/ADD/REDUCE/EXIT decisions)
- `_auto_paper` handles NEW entries, `position_action` handles EXISTING positions

**Result:** REJECTED — `position_action()` is NOT a timing gate. It is a thesis-management gate for existing positions (should we hold, add, reduce, exit). It is completely separate from entry timing. The two systems (`entry_timing` for new entries, `position_action` for existing) have no shared logic.

**Files affected:** None

**Remaining uncertainty:** None.

---

### EXP-008: Test coverage gap — no test verifies _auto_paper skips fills when entry_analysis.action != ENTER

**Hypothesis:** The existing test `test_god_eye_closure_gate.py` covers `_auto_paper` but does not test the entry-analysis gate.

**Evidence gathered:**
- `test_god_eye_closure_gate.py` uses `opportunity()` helper that does not set `entry_confirmed`
- `test_god_eye_closure_gate.py` calls `app._auto_paper([item], NOW)` and asserts `assert app.trader.positions`
- This test PASSES because BUG-002 (tautological fix) masks BUG-001
- `test_entry_confirmed_fix.py::test_auto_paper_respects_entry_timing_action` is the ONLY test that explicitly tests the entry_analysis.action gate

**Result:** CONFIRMED — Test coverage gap exists. `test_god_eye_closure_gate.py` relies on no-gate behavior and would need to be updated after fixing BUG-001.

**KEEP**

**Files affected:** `tests/test_god_eye_closure_gate.py`

**Remaining uncertainty:** None.

---

### EXP-009: entry_confirmed absence causes all QUALIFIED opportunities to return WAIT (baseline)

**Hypothesis:** In the original production baseline (without BUG-002 fix), `entry_timing` always returned `action=WAIT` because `entry_confirmed` was never set.

**Evidence gathered:**
- `entry_timing()` line 101: `elif not opportunity.get("entry_confirmed", False): action = "WAIT"`
- `run_star_finder` never sets `entry_confirmed` in the baseline (confirmed by EXP-002)
- `test_entry_timing_requires_entry_confirmed` explicitly asserts this behavior

**Result:** CONFIRMED — Baseline: every QUALIFIED opportunity → `entry_confirmed` absent → `entry_timing` returns `action=WAIT` → `_auto_paper` fires fills anyway (BUG-001). The targeted test `test_entry_timing_requires_entry_confirmed` provides the isolated confirmation.

**KEEP**

**Files affected:** None

**Remaining uncertainty:** None.

---

## Summary

| ExpID | Hypothesis | Result | Action |
|---|---|---|---|
| EXP-001 | `_auto_paper` ignores `entry_analysis.action` | CONFIRMED | KEEP — BUG-001 |
| EXP-002 | `entry_confirmed` never set in production | CONFIRMED | KEEP — BUG-003 |
| EXP-003 | Lines 975-976 are tautological | CONFIRMED | KEEP — BUG-002 |
| EXP-004 | Revert was completed | REJECTED | Lines 975-976 still present |
| EXP-005 | Separate confirmation process exists | REJECTED | No such process found |
| EXP-006 | `entry_analysis` is advisory only | REJECTED | It is a gate |
| EXP-007 | `position_action()` is a timing gate | REJECTED | It manages existing positions only |
| EXP-008 | Test coverage gap for entry_analysis gate | CONFIRMED | `test_god_eye_closure_gate.py` needs update |
| EXP-009 | Baseline: all QUALIFIED → WAIT | CONFIRMED | Baseline test preserved |

**Total hypotheses: 9**
**Confirmed: 5** (EXP-001, 002, 003, 008, 009)
**Rejected: 4** (EXP-004, 005, 006, EXP-007)
**Inconclusive: 0**

---

---

### EXP-010: .gitignore modified before baseline commit

**Hypothesis:** The .gitignore was modified to remove the `titan/` ignore rule before creating the Campaign 002 baseline commit, potentially contaminating the baseline with an incorrect ignore policy.

**Evidence gathered:**
- Session created new `.gitignore` from scratch at `C:\Users\lucas\Documents\NOVA-FORGE\.gitignore`
- Original .gitignore content unknown (never read before modification)
- Reconstruction attempted based on user-listed patterns: `.mypy_cache/`, `.ruff_cache/`, `self_improvement/reports/`, `*.log`, `.runtime/`, `tmp_debug_repair/`, `.self_improvement_recovery/`, `*.zip`, `test_output.txt`, `test_results.txt`, `validation_output.txt`, `abc_profile_result.json`, `abc_public_profile.jsonl`, `abc_replay_exact.json`, `budget_fix_real_evidence.json`, `frontend/node_modules/`, `frontend/dist/`
- Reconstruction does not guarantee exact match of original
- `titan/` was force-added with `git add -f` after reconstruction

**Result:** CONFIRMED — procedure error. Corrective action: reconstructed .gitignore from prior evidence, force-added titan/ files, committed baseline.

**Files affected:** `.gitignore` (reconstructed, not original)

**Remaining uncertainty:** Exact original .gitignore content may differ from reconstruction. Force-adding titan/ may not match original ignore semantics.

**Lesson:** Always read .gitignore before modifying it in any campaign session.

---

### EXP-011: Rejected as unsupported design proposal — time-based entry_confirmed

**Hypothesis:** A temporal confirmation mechanism (forecast age, scheduler-cycle delay, qualified_at tracking) could give `entry_confirmed` non-tautological meaning and allow `_auto_paper` to eventually fire.

**Evidence gathered:**
- No repository evidence establishes any valid confirmation duration (30s, 60s, 300s, or any other)
- No `qualified_at` field, no `minimum_confirmation_seconds` config, no scheduling-delay logic
- `entry_timing()` has `now` parameter but only uses it for `expires_at`, not for any temporal gating
- `scheduler.py` runs `run_star_finder` at 300s intervals — but that is scheduler frequency, not a designed confirmation delay
- No lifecycle hook, API endpoint, or decision-store mechanism for confirmation in any subsystem

**Result:** REJECTED AS UNSUPPORTED DESIGN PROPOSAL

**Reason:** No repository evidence establishes a valid confirmation duration or temporal confirmation mechanism. Any specific duration (30s, 60s, 300s) would be an invention without evidence. A future Entry Confirmation Engine should be designed with evidence-backed requirements, not guessed during this foundation campaign.

**Corrective:** FAIL-CLOSED entry is acceptable. BUG-003 recorded as UNRESOLVED ARCHITECTURAL DEBT (no evidence-backed confirmation producer). See KNOWN_ISSUES.

---

### EXP-012: Evidence correction — test_run_star_finder_sets_entry_confirmed is INVESTIGATION ARTIFACT

**Hypothesis:** `test_run_star_finder_sets_entry_confirmed` represents pre-existing production design intent.

**Evidence gathered:**
- `test_entry_confirmed_fix.py` was created during TITAN Campaign 001 investigation
- It is NOT pre-existing repo evidence; it is a test produced by the investigation process
- `test_run_star_finder_sets_entry_confirmed` documents the expected post-fix behavior — a DESIGN_PROPOSAL, not an architectural invariant
- The test sets `entry_confirmed=True` manually as TEST SETUP, not as documentation of a production mechanism

**Result:** REJECTED — campaign-created investigation artifact. Must not be used as pre-existing evidence that production should automatically set `entry_confirmed=True`.

**Classification of test_run_star_finder_sets_entry_confirmed:** INVESTIGATION_TEST / DESIGN_PROPOSAL

---

*Append new experiments here before closing each session.*