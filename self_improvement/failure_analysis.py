"""Deterministic failure classification, judging, and retry planning."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class FailureAnalysis:
    type: str
    reason: str
    retryable: bool
    suggested_change: str


@dataclass
class JudgeResult:
    accepted: bool
    decision: str
    reason: str
    tests_status: str
    syntax_status: str
    relevance: object | None = None


@dataclass
class AttemptRecord:
    iteration: int
    strategy: str
    files: list[str]
    patch_fingerprint: str | None
    result: str
    failure_type: str | None = None
    failure_reason: str | None = None
    tests_status: str = "not_run"
    relevance_status: str = "not_run"

    def as_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "strategy": self.strategy,
            "files": list(self.files),
            "patch_fingerprint": self.patch_fingerprint,
            "result": self.result,
            "failure_type": self.failure_type,
            "failure_reason": self.failure_reason,
            "tests_status": self.tests_status,
            "relevance_status": self.relevance_status,
        }


def patch_fingerprint(original: str, new_content: str) -> str:
    diff = "\n".join(
        line for line in __import__("difflib").unified_diff(
            original.splitlines(), new_content.splitlines(), lineterm=""
        )
    )
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def analyze_failure(error: str | None, *, relevance_reason: str | None = None) -> FailureAnalysis:
    reason = (
        relevance_reason
        if relevance_reason == "no_change"
        else error or relevance_reason or "unknown failure"
    )
    text = reason.casefold()
    if relevance_reason:
        failure_type = relevance_reason
        retryable = relevance_reason not in {"no_change"}
        suggestion = "modify the explicitly requested symbol or identifier; do not introduce unrelated symbols"
    elif "patch_provenance_mismatch" in text or "stale_content" in text or "precondition_failed" in text:
        failure_type, retryable, suggestion = "patch_application_failure", False, "regenerate from the exact current file content"
    elif "patch_target_failure" in text:
        failure_type, retryable, suggestion = "patch_target_failure", True, "use T1 and an existing symbol from the canonical target"
    elif "patch_contract:" in text or "invalid_patch_response" in text or "invalid_new_file_response" in text:
        failure_type, retryable, suggestion = "protocol_failure", True, "repair only the structured response contract"
    elif "generation_failed" in text or "model_generation" in text:
        failure_type, retryable, suggestion = "generation_failure", True, "retry generation without changing the requested behavior"
    elif any(marker in text for marker in ("symbol_not_found", "symbol_ambiguous", "anchor_not_found", "anchor_ambiguous", "invalid_target", "ambiguous_target")):
        failure_type, retryable, suggestion = "patch_validation_failure", True, "select one exact target from the authorized file context"
    elif "syntax" in text or "invalid syntax" in text:
        failure_type, retryable, suggestion = "syntax_error", True, "preserve the original objective and repair only syntax"
    elif "test" in text or "assert" in text:
        failure_type, retryable, suggestion = "test_failure", True, "use the test failure as a constraint for a targeted fix"
    elif "review_requested_changes" in text or "review_failure" in text:
        failure_type, retryable, suggestion = "review_failure", True, "address only the review concerns"
    elif "autorisé" in text or "exclu" in text or "safety" in text:
        failure_type, retryable, suggestion = "safety_rejection", False, "do not broaden the permitted file scope"
    elif "n'existe pas" in text or "anchor_not_found" in text or "symbol_not_found" in text:
        failure_type, retryable, suggestion = "patch_not_applicable", True, "use a unique symbol or anchor structured edit"
    else:
        failure_type, retryable, suggestion = "unknown_failure", True, "change the approach and preserve the original objective"
    return FailureAnalysis(failure_type, reason, retryable, suggestion)


def plan_retry(task: str, analysis: FailureAnalysis, history: list[dict], previous_strategy: str) -> tuple[str, str]:
    strategies = {
        "direct_replace": "symbol_edit",
        "symbol_edit": "anchor_edit",
        "anchor_edit": "targeted_fix",
        "targeted_fix": "test_guided_fix",
        "test_guided_fix": "relevance_guided_fix",
        "relevance_guided_fix": "targeted_fix",
    }
    strategy = strategies.get(previous_strategy, "targeted_fix")

    # Build failure-type-specific guidance so the model makes a substantive change.
    extra = ""
    if analysis.type in ("relevance_missing_required_identifier", "irrelevant_patch"):
        extra = (
            "\nRELEVANCE GUIDANCE: the patch must contain the exact function/class "
            "name(s) mentioned in the task as Python definitions (def name / class name). "
            "When using replace_symbol_block the new_text must start with the full "
            "signature: decorators (if any) + 'def name(...)' or 'class name' line, "
            "then the complete body with correct indentation. "
            "Do NOT include just the body without the def/class line."
        )
    elif analysis.type == "syntax_error":
        extra = (
            "\nSYNTAX GUIDANCE: the previous new_text produced invalid Python. "
            "When using replace_symbol_block, new_text must be the complete, "
            "standalone block: include every decorator, the def/class line, and the "
            "full body. Verify indentation is consistent (4 spaces per level)."
        )
    elif analysis.type == "relevance_target_symbol_unchanged":
        extra = (
            "\nGUIDANCE: the requested symbol exists but was not modified. "
            "Replace its body/return value to satisfy the task requirement."
        )
    elif analysis.type == "protocol_failure":
        extra = (
            "\nOPERATION FORMAT GUIDANCE: every operation must be valid JSON with required fields. "
            "For replace_symbol_block: include 'symbol' (function/class name) and 'new_text'. "
            "For replace: include 'old_text' (exact code to replace) and 'new_text'. "
            "For insert_after_anchor: include 'anchor' (exact unique context) and 'new_text'."
        )

    instruction = (
        f"{task}\n"
        f"Failure analysis: {analysis.type}\n"
        f"Why the previous attempt failed: {analysis.reason}\n"
        f"Previous strategy: {previous_strategy}\n"
        f"New strategy: {strategy}\n"
        f"Constraint: {analysis.suggested_change}. Do not reproduce the previous patch."
        f"{extra}"
    )
    return strategy, instruction


def judge_patch(*, changed: bool, syntax_ok: bool, relevance, tests_requested: bool, tests_passed: bool, failure: FailureAnalysis | None = None) -> JudgeResult:
    if not changed:
        return JudgeResult(False, "REJECT", "no_change", "not_run", "valid" if syntax_ok else "invalid", relevance)
    if not syntax_ok:
        return JudgeResult(False, "REJECT", "syntax_error", "not_run", "invalid", relevance)
    if relevance is not None and not relevance.passed:
        return JudgeResult(False, "REJECT", relevance.reason or "relevance_failure", "not_run", "valid", relevance)
    if failure is not None:
        return JudgeResult(False, "REJECT", failure.type, "failed", "valid", relevance)
    if tests_requested and tests_passed:
        return JudgeResult(True, "ACCEPT", "tests_passed_and_relevant", "passed", "valid", relevance)
    if tests_requested:
        return JudgeResult(False, "REJECT", "test_failure", "failed", "valid", relevance)
    return JudgeResult(True, "ACCEPT", "accepted_without_tests", "not_run", "valid", relevance)
