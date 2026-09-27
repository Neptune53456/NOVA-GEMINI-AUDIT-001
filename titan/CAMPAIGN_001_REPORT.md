# TITAN Campaign 001 — Final Report

**Repository:** `C:\Users\lucas\Documents\NOVA-FORGE`
**Campaign:** `titan/`
**Date:** 2026-09-XX (resumed after interruption)
**Session:** Claude Code — resumed, pre-intervention state inspected

---

## Executive Summary

A semantic investigation into the `entry_confirmed` / `entry_analysis.action` signal path in the Star Finder AUTO_PAPER pipeline revealed **two independent defects** that together allow `_auto_paper` to fire fills without respecting the entry-timing gate.

---

## Verified Bugs

### BUG-001: `_auto_paper` ignores `entry_analysis.action` — fills fire regardless of timing gate

| Field | Value |
|---|---|
| Severity | HIGH — bypasses safety gate |
| Subsystem | `nova_api/god_eye/service.py`, method `_auto_paper` (lines 486–522) |
| File | `service.py:498-521` |
| Status | **OPEN — NOT FIXED** |
| Reproduction | `tests/test_entry_confirmed_fix.py::test_auto_paper_respects_entry_timing_action` — Case 1 |

**Finding:** `_auto_paper` gates execution on `status == "QUALIFIED"` and `risk_approved` but never checks `entry_analysis["action"]`. The `entry_timing()` call at line 977 computes the gate correctly but the result is stored and never consulted before the execution block.

```python
# service.py:498-521 — execution block, NO entry_analysis.action check
if opportunity.get("status")!="QUALIFIED": continue
if opportunity.get("risk_approved") is False or self.trader_kill_switch: continue
# ... proceeds to execute fill — entry_analysis.action is IGNORED
result = self.execution_engine.execute(request, ...)
```

**Evidence chain:**
1. `entry_timing()` (`star_finder.py:98-109`) returns `{action: "ENTER"|"WAIT"|"IGNORE", reasons: [...]}`
2. `run_star_finder` calls `entry_timing()` at `service.py:977` and stores result as `opportunity["entry_analysis"]`
3. `service.py:997`: `auto_actions = self._auto_paper(built, now)`
4. `_auto_paper` iterates `opportunities` from `built` (which already have `entry_analysis`) — no check of `entry_analysis["action"]` before firing fills
5. Targeted baseline test (`test_entry_confirmed_fix.py:124-146`) confirms: `_auto_paper` fires fills for an opportunity with `entry_analysis["action"] == "WAIT"`

**Fix required:** Add before the execution block:
```python
if opportunity.get("entry_analysis", {}).get("action") != "ENTER": continue
```

---

### BUG-002: Tautological `entry_confirmed=True` assignment — no independent signal

| Field | Value |
|---|---|
| Severity | MEDIUM — architectural debt / semantic defect |
| Subsystem | `nova_api/god_eye/service.py`, `run_star_finder` (lines 975-976) |
| File | `service.py:975-976` |
| Status | **OPEN — revert intended but not applied** |

**Finding:** The current code sets `entry_confirmed = True` derived directly from `status == "QUALIFIED"`:

```python
if opportunity["status"]=="QUALIFIED":
    opportunity["entry_confirmed"]=True
```

Since `QUALIFIED` is defined as "passed all rejection gates" (set by `rejection_gates()` — `star_finder.py:51-76`), and `entry_confirmed` is the input to `entry_timing()` that gates on QUALIFIED → action=ENTER, this assignment adds **no additional filter** — it is trivially satisfied by the same condition that already produced QUALIFIED. The `entry_confirmed` dimension is entirely redundant with the `status` dimension.

**Pre-intervention state:** In the original production baseline, `entry_confirmed` was never set anywhere. `run_star_finder` built opportunities through `rejection_gates` → `status=QUALIFIED` → `entry_timing()` which always returned `action=WAIT` because `entry_confirmed` was absent. `_auto_paper` filled anyway (BUG-001).

**Current state:** Lines 975-976 still present (revert not applied before interruption).

**Intended revert:** Remove lines 975-976.

**Note on the correct production design:** See Section 6 — the correct fix for BUG-001 (gating in `_auto_paper`) eliminates the need for BUG-002 entirely, because `_auto_paper` would enforce `entry_analysis.action == "ENTER"` regardless of how `entry_confirmed` is set.

---

### BUG-003: No production mechanism produces `entry_confirmed=True`

| Field | Value |
|---|---|
| Severity | LOW — dead dimension, but impact contained by BUG-002 masking |
| Subsystem | All of production |
| Status | **OPEN** |

**Finding:** `entry_confirmed` is a field in the opportunity schema (persisted by `storage.py:282-295`) but is never set by any production mechanism other than the tautological fix (BUG-002). No API endpoint, no lifecycle transition, no decision logic, no user action sets `entry_confirmed` in production. If BUG-002 is reverted without also addressing BUG-001, every QUALIFIED opportunity would have `entry_analysis["action"] == "WAIT"` and `_auto_paper` would be fully gated — but the root cause of that gating would be accidental (BUG-002 removal), not deliberate.

---

## Test Coverage Analysis

| Test | Coverage | Current behavior |
|---|---|---|
| `tests/test_entry_confirmed_fix.py::test_entry_timing_requires_entry_confirmed` | `entry_timing()` in isolation | **PASSES** — confirms baseline: no `entry_confirmed` → action=WAIT |
| `tests/test_entry_confirmed_fix.py::test_entry_timing_enter_when_confirmed` | `entry_timing()` in isolation | **PASSES** — confirms: `entry_confirmed=True` → action=ENTER |
| `tests/test_entry_confirmed_fix.py::test_auto_paper_respects_entry_timing_action` | `_auto_paper` with entry_analysis | **FAILING (baseline bug confirmed)** — Case 1 fires fills for action=WAIT |
| `tests/test_entry_confirmed_fix.py::test_run_star_finder_sets_entry_confirmed` | `run_star_finder` + entry_timing | **DOCUMENTARY** — describes intended post-fix behavior |
| `tests/test_star_finder.py::test_entry_enter_wait_ignore` | `entry_timing()` unit test | **PASSES** — correct function behavior |
| `tests/test_god_eye_closure_gate.py::test_auto_paper_entry_exit_restart_resolution_and_replay` | Full closure cycle | **PASSES** — relies on no-gate behavior (BUG-001 masked by BUG-002) |

**Baseline evidence preserved:** `test_auto_paper_respects_entry_timing_action` Case 1 (lines 124-146 of `test_entry_confirmed_fix.py`) explicitly asserts:
```python
assert False, (
    f"BASELINE BUG CONFIRMED: _auto_paper executed {len(fills_no_confirm)} fill(s) "
    f"for an opportunity with action=WAIT (entry_confirmed absent). "
    f"The entry timing gate is completely broken — entry_analysis.action is never checked."
)
```
This is the **verified reproduction** of BUG-001.

---

## Rejected Hypotheses

### H-REJECTED-1: "The revert of lines 975-976 was completed before interruption"

Evidence: `service.py:975-976` still contains the tautological fix:
```python
if opportunity["status"]=="QUALIFIED":
    opportunity["entry_confirmed"]=True
```
The file was read directly and these lines are confirmed present. The revert was not applied.

---

### H-REJECTED-2: "`entry_confirmed` should be set by a separate confirmation process (human review, delayed confirmation, etc.)"

Evidence: The test `test_run_star_finder_sets_entry_confirmed` explicitly states the production fix is for `run_star_finder` to set `entry_confirmed=True` for QUALIFIED opportunities. The test name `test_entry_confirmed_fix.py` and its docstring confirm this. There is no indication in the codebase of a separate human review step or delayed confirmation process — `run_star_finder` is a fully automated scheduler task.

---

### H-REJECTED-3: "`entry_analysis.action` is advisory information only"

Evidence: `entry_timing()` is the ONLY consumer of `entry_confirmed`. The function returns three action states (ENTER, WAIT, IGNORE) with semantically meaningful reasons. The `["entry_confirmation_pending"]` reason for WAIT state is designed to explain why entry is gated. A pure advisory signal would not need to produce IGNORE (from rejection_reasons) as a separate action — it would just provide data. The function is named `entry_timing` and its docstring and design clearly indicate it is a gate function.

---

## Scientific Integrity

**No integrity violations detected.**

- No TRAIN/VALIDATION/HOLDOUT contamination in this investigation.
- No benchmark data was used to guide the investigation.
- No hardcoded identifiers or score manipulation.
- The targeted test (`test_entry_confirmed_fix.py`) was preserved as baseline evidence.
- No changes were made to production code during this investigation (inspection only, per AGENTS.md scope).

---

## Architectural Debt

### AD-001: Redundant dimensions in opportunity state

`status` (QUALIFIED/REJECTED) and `entry_confirmed` (bool) are currently redundant in this paper-only, fully automated system. A QUALIFIED opportunity has already passed all hard gates. Setting `entry_confirmed = True` for every QUALIFIED opportunity adds no semantic value. The architecture should either:
- **Option A (simpler):** Remove `entry_confirmed` entirely and gate entry purely on `entry_analysis.action` (which is computed from other fields), OR
- **Option B (if保留):** Give `entry_confirmed` independent meaning — e.g., set it only after a time delay, a secondary validation, or a human confirmation step.

### AD-002: `_auto_paper` has no entry-timing gate

The execution engine `_auto_paper` makes no reference to `entry_analysis` before filling. This is the primary execution gate bypass.

### AD-003: `position_action()` in `trader.py:303` does not consult `entry_analysis`

The re-evaluation path (`reevaluate_paper_positions`) uses `position_action()` rather than `entry_analysis`. While this is a different concern (post-entry management vs. initial entry), the architecture is inconsistent — `entry_timing` and `position_decision` are separate systems without shared state.

### AD-004: `entry_confirmed` not validated at persistence

The `storage.py:save_star_opportunity` (line 282) serializes the full opportunity dict including `entry_confirmed` and `entry_analysis` as JSON — no schema validation, no type checking, no required-field enforcement. An opportunity could be persisted with `entry_confirmed=None` or `entry_analysis=None` without error.

---

## Files Changed

No files were changed during this investigation (inspection-only, per AGENTS.md). This report documents the **current pre-intervention state**.

| File | Change | Status |
|---|---|---|
| `nova_api/god_eye/service.py` | Lines 975-976 contain tautological fix — revert pending | **PENDING REVERT** |
| `tests/test_entry_confirmed_fix.py` | Created in prior session — baseline test preserved | **INTACT** |
| `tests/test_god_eye_closure_gate.py` | No changes | **INTACT** |

---

## Tests and Results

| Test | Result | Notes |
|---|---|---|
| `tests/test_entry_confirmed_fix.py::test_entry_timing_requires_entry_confirmed` | **PASS** | Confirms baseline: no `entry_confirmed` → action=WAIT |
| `tests/test_entry_confirmed_fix.py::test_entry_timing_enter_when_confirmed` | **PASS** | Confirms fix: `entry_confirmed=True` → action=ENTER |
| `tests/test_entry_confirmed_fix.py::test_auto_paper_respects_entry_timing_action` | **FAIL (baseline confirmed)** | Case 1: fills fire for action=WAIT — BUG-001 verified |
| `tests/test_entry_confirmed_fix.py::test_run_star_finder_sets_entry_confirmed` | **PASS (documentary)** | Documents intended post-fix behavior |
| `tests/test_star_finder.py::test_entry_enter_wait_ignore` | **PASS** | Correct function behavior |

Run command: `pytest tests/test_entry_confirmed_fix.py tests/test_star_finder.py -v --no-cov`

---

## Remaining Risks

1. **Reverting BUG-002 (lines 975-976) alone is insufficient.** Without fixing BUG-001, `_auto_paper` would still fire fills — but now `entry_analysis.action` would be WAIT for every QUALIFIED opportunity. This would halt ALL paper entries accidentally (not as a deliberate fix, but as a side effect of removing BUG-002). The correct first fix is BUG-001 (add `entry_analysis.action` gate in `_auto_paper`).
2. **`test_god_eye_closure_gate.py` relies on no-gate behavior.** After fixing BUG-001, those tests will need to provide `entry_analysis["action"] = "ENTER"` for their opportunities (or the test helper needs to set `entry_confirmed=True` explicitly).
3. **No mechanism exists to set `entry_confirmed` in production** outside the tautological fix. If BUG-002 is reverted AND BUG-001 is fixed, paper entries will halt unless `run_star_finder` is modified to set `entry_confirmed=True` non-tautologically, or the `_auto_paper` gate is changed to not depend on `entry_confirmed` at all.
4. **The `entry_confirmed` field in storage is not validated.** Malformed or absent values are silently accepted.

---

## Recommended Next Campaign

**TITAN-002: Fix entry-timing gate and clean up architectural debt**

Priority order:
1. **Fix BUG-001** — Add `entry_analysis.action` gate to `_auto_paper`
2. **Revert BUG-002** — Remove tautological `entry_confirmed=True` assignment
3. **Fix BUG-003** — Decide on `entry_confirmed` semantics; implement correct production signal OR remove the field
4. **Update `test_god_eye_closure_gate.py`** — ensure tests provide correct entry_analysis for their opportunities
5. **Add schema validation** for `entry_confirmed` in `save_star_opportunity`

---

## Proposed High-Value ASTRA Milestones

### ASTRA-001: Entry-Timing Gate Enforcement
**Subsystem:** god_eye / AUTO_PAPER
**Problem:** `_auto_paper` fires fills without checking entry timing signal
**Proposed scope:** Add `entry_analysis.action` gate in `_auto_paper`, add test, update closure gate tests
**Dependencies:** None
**Risks:** Low — targeted, isolated change
**Expected engineering benefit:** Safety gate prevents premature entries; correct behavior matches designed signal path
**Expected benefit evidence:** NONE — no profitability claims without validated A/B test on historical data

### ASTRA-002: Simplify opportunity state machine
**Subsystem:** god_eye / star_finder / opportunity schema
**Problem:** `status` (QUALIFIED) and `entry_confirmed` (bool) are redundant in fully automated paper trading
**Proposed scope:** Either remove `entry_confirmed` entirely (and gate on `entry_analysis.action`), or give it independent meaning (delayed confirmation, secondary validation)
**Dependencies:** ASTRA-001
**Risks:** Medium — requires understanding of all consumers of `entry_confirmed`
**Expected engineering benefit:** Cleaner state machine, reduced accidental coupling

### ASTRA-003: Validate opportunity schema at persistence
**Subsystem:** god_eye / storage
**Problem:** `save_star_opportunity` accepts any dict without validating required fields
**Proposed scope:** Add dataclass/pydantic schema for opportunity; validate at `save_star_opportunity`
**Dependencies:** None
**Risks:** Low — additive validation
**Expected engineering benefit:** Catch bad data early; better error messages