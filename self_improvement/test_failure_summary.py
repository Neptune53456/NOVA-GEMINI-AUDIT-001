"""Deterministic, bounded diagnostics for public-test candidate failures."""

from __future__ import annotations

import json
import difflib
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable


class FailureCategory(str, Enum):
    SYNTAX_FAILURE = "SYNTAX_FAILURE"
    IMPORT_FAILURE = "IMPORT_FAILURE"
    TYPE_FAILURE = "TYPE_FAILURE"
    ASSERTION_FAILURE = "ASSERTION_FAILURE"
    BEHAVIOR_MISMATCH = "BEHAVIOR_MISMATCH"
    WRONG_TARGET = "WRONG_TARGET"
    INCOMPLETE_IMPLEMENTATION = "INCOMPLETE_IMPLEMENTATION"
    REGRESSION = "REGRESSION"
    TEST_INFRA_FAILURE = "TEST_INFRA_FAILURE"


class RepairProgress(str, Enum):
    STRONG_PROGRESS = "strong_progress"
    PARTIAL_PROGRESS = "partial_progress"
    NO_PROGRESS = "no_progress"
    REGRESSION = "regression"


@dataclass(frozen=True)
class TestFailureSummary:
    __test__ = False
    test_name: str | None = None
    file: str | None = None
    symbol: str | None = None
    line: int | None = None
    failure_type: str = FailureCategory.TEST_INFRA_FAILURE.value
    assertion: str | None = None
    expected: str | None = None
    actual: str | None = None
    exception: str | None = None
    traceback_excerpt: str = ""
    candidate_changed_symbols: list[str] = field(default_factory=list)
    likely_relation_to_patch: str = "unknown"
    failed_tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FAILED_TEST = re.compile(r"(?:FAILED\s+)?((?:[\w./\\-]+\.py)::[\w\[\].:/-]+)")
_LOCATION = re.compile(r"(?P<file>[\w./\\-]+\.py):(?P<line>\d+)(?::\s+in\s+(?P<symbol>[\w.]+))?")


def _category(text: str) -> FailureCategory:
    folded = text.casefold()
    if "test_timeout_after_" in folded or "test_infra_failure" in folded or "filenotfounderror" in folded:
        return FailureCategory.TEST_INFRA_FAILURE
    if "syntaxerror" in folded or "syntax_error" in folded:
        return FailureCategory.SYNTAX_FAILURE
    if "importerror" in folded or "modulenotfounderror" in folded or "import_failure" in folded:
        return FailureCategory.IMPORT_FAILURE
    if "typeerror" in folded:
        return FailureCategory.TYPE_FAILURE
    if "assertionerror" in folded or re.search(r"(?m)^e\s+assert\s", folded):
        return FailureCategory.BEHAVIOR_MISMATCH if "expected" in folded and "actual" in folded else FailureCategory.ASSERTION_FAILURE
    if "fixture" in folded or "pytest" in folded and "not found" in folded:
        return FailureCategory.TEST_INFRA_FAILURE
    if "wrong_target" in folded or "target symbol unchanged" in folded:
        return FailureCategory.WRONG_TARGET
    if "notimplementederror" in folded or "incomplete" in folded:
        return FailureCategory.INCOMPLETE_IMPLEMENTATION
    return FailureCategory.TEST_INFRA_FAILURE


def parse_test_failure(output: str, *, changed_symbols: Iterable[str] = (), max_excerpt_chars: int = 2400) -> TestFailureSummary:
    """Extract only deterministic public failure data; never asks a model."""
    text = (output or "").replace("\x00", "")
    tests = list(dict.fromkeys(match.group(1).replace("\\", "/") for match in _FAILED_TEST.finditer(text)))
    location = next(iter(_LOCATION.finditer(text)), None)
    assertion = next((line.strip()[2:].strip() for line in text.splitlines() if line.lstrip().startswith("E assert")), None)
    expected = actual = None
    normalized_assertion = re.sub(r"^assert\s+", "", assertion or "")
    match = re.search(r"(?P<actual>.+?)\s*==\s*(?P<expected>.+)", normalized_assertion)
    if match:
        expected, actual = match.group("expected").strip()[:500], match.group("actual").strip()[:500]
    else:
        match = re.search(r"expected\s*[:=]?\s*(?P<expected>.+?)(?:,|\s+)actual\s*[:=]?\s*(?P<actual>.+)", text, re.I)
        if match:
            expected, actual = match.group("expected").strip()[:500], match.group("actual").strip()[:500]
    exception = next((line.strip()[2:].strip() for line in text.splitlines()
                      if line.lstrip().startswith("E ") and not line.lstrip().startswith("E assert")), None)
    changed = list(dict.fromkeys(str(item) for item in changed_symbols))
    file_name = location.group("file").replace("\\", "/") if location else None
    related = "unknown"
    if file_name and any(file_name in item or item.split(":", 1)[0].endswith(file_name) for item in changed):
        related = "direct"
    elif changed and _category(text) in {FailureCategory.ASSERTION_FAILURE, FailureCategory.BEHAVIOR_MISMATCH}:
        related = "plausible"
    return TestFailureSummary(
        test_name=tests[0] if tests else None, file=file_name,
        symbol=location.group("symbol") if location else None,
        line=int(location.group("line")) if location else None,
        failure_type=_category(text).value, assertion=assertion, expected=expected, actual=actual,
        exception=exception, traceback_excerpt=text[-max_excerpt_chars:],
        candidate_changed_symbols=changed, likely_relation_to_patch=related, failed_tests=tests,
    )


def compare_failures(before: TestFailureSummary, after: TestFailureSummary) -> RepairProgress:
    old, new = set(before.failed_tests), set(after.failed_tests)
    if old and not new:
        return RepairProgress.STRONG_PROGRESS
    if new - old:
        return RepairProgress.REGRESSION
    if len(new) < len(old):
        return RepairProgress.PARTIAL_PROGRESS
    before_signature = (before.failure_type, before.test_name, before.assertion, before.exception)
    after_signature = (after.failure_type, after.test_name, after.assertion, after.exception)
    return RepairProgress.NO_PROGRESS if before_signature == after_signature else RepairProgress.PARTIAL_PROGRESS


def _compact_previous_patch(previous_patch: list[dict[str, str]], *, per_file_chars: int = 12000, total_chars: int = 24000) -> list[dict[str, str]]:
    """Keep semantic-repair context focused on changed hunks, not whole files."""
    compact: list[dict[str, str]] = []
    remaining = max(1000, int(total_chars))
    for row in previous_patch:
        if remaining <= 0:
            break
        file_name = str(row.get("file", ""))
        before = str(row.get("before", ""))
        after = str(row.get("after", ""))
        diff = "\n".join(difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f"{file_name}:before", tofile=f"{file_name}:after",
            n=4, lineterm="",
        ))
        if not diff:
            continue
        limit = min(max(1000, int(per_file_chars)), remaining)
        excerpt = diff[:limit]
        if len(diff) > limit:
            excerpt += "\n...<diff truncated>"
        compact.append({"file": file_name, "diff": excerpt})
        remaining -= len(excerpt)
    return compact


def build_repair_instruction(*, objective: str, previous_patch: list[dict[str, str]], summary: TestFailureSummary,
                             target_symbols: Iterable[str], allowed_files: Iterable[str], constraints: Iterable[str]) -> str:
    target_list = list(target_symbols)
    # Machine-readable marker consumed by Repository/Patch targeting before the
    # free-form payload. Values are narrowed to the qualified symbol portion so
    # source code embedded in previous_patch cannot accidentally expand the
    # canonical target set during a semantic repair.
    canonical_symbols = []
    for raw in target_list:
        value = str(raw).rsplit(":", 1)[-1].strip()
        if value and value not in canonical_symbols:
            canonical_symbols.append(value)
    payload = {
        "objective": objective,
        "previous_patch": _compact_previous_patch(previous_patch),
        "test_failure_summary": summary.to_dict(),
        "target_symbols": target_list,
        "allowed_files": list(allowed_files),
        "patch_constraints": list(constraints),
    }
    return ("SEMANTIC_PATCH_REPAIR (unique attempt). Correct the existing patch only. Keep the same objective, "
            "files, and code area; return a genuinely different patch.\n"
            "CANONICAL_TARGET_SYMBOLS: " + json.dumps(canonical_symbols, ensure_ascii=False) + "\n" +
            json.dumps(payload, ensure_ascii=False, sort_keys=True))
