"""TRAIN-only, sanitized forensics for minimal Planner decisions."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from self_improvement.experiment_memory import sanitize_text


class PlannerForensicsAccessDenied(ValueError):
    pass


def _require_train(split: str) -> None:
    if str(split).casefold() != "train":
        raise PlannerForensicsAccessDenied("Planner forensics may access TRAIN only")


def _short(value: Any, limit: int = 500) -> str:
    text = sanitize_text(str(value or ""))
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=[REDACTED]",
        text,
    )
    return text[:limit]


def decision_summary(data: Any) -> dict[str, Any]:
    """Keep structure and short intents, never a full provider response."""
    if not isinstance(data, dict):
        return {"root_type": type(data).__name__, "fields": [], "actions": []}
    actions = []
    raw_actions = data.get("actions")
    if isinstance(raw_actions, list):
        for index, raw in enumerate(raw_actions[:12], start=1):
            if not isinstance(raw, dict):
                actions.append({"index": index, "value_type": type(raw).__name__})
                continue
            actions.append({
                "index": index,
                "fields": sorted(str(key)[:80] for key in raw)[:16],
                "action_type": _short(raw.get("action_type"), 80),
                "target_reference": _short(raw.get("target_reference"), 80),
                "intent": _short(raw.get("intent"), 300),
                "dependencies": [_short(item, 80) for item in raw.get("dependencies", [])[:8]]
                    if isinstance(raw.get("dependencies"), list) else [],
                "test_intent": _short(raw.get("test_intent"), 240),
            })
    return {
        "root_type": "object",
        "version": _short(data.get("version"), 80),
        "fields": sorted(str(key)[:80] for key in data)[:24],
        "actions": actions,
    }


@dataclass(frozen=True)
class DecisionDiff:
    actions_added: tuple[int, ...] = ()
    actions_removed: tuple[int, ...] = ()
    actions_changed: tuple[int, ...] = ()
    targets_changed: tuple[int, ...] = ()
    intents_changed: tuple[int, ...] = ()
    dependencies_changed: tuple[int, ...] = ()

    @property
    def changed(self) -> bool:
        return any(asdict(self).values())


def decision_diff(before: Any, after: Any) -> DecisionDiff:
    left = decision_summary(before).get("actions", [])
    right = decision_summary(after).get("actions", [])
    common = min(len(left), len(right))
    changed, targets, intents, dependencies = [], [], [], []
    for index in range(common):
        a, b = left[index], right[index]
        fields = ("action_type", "target_reference", "intent", "dependencies", "test_intent")
        if any(a.get(field) != b.get(field) for field in fields):
            changed.append(index + 1)
        if a.get("target_reference") != b.get("target_reference"):
            targets.append(index + 1)
        if a.get("intent") != b.get("intent") or a.get("test_intent") != b.get("test_intent"):
            intents.append(index + 1)
        if a.get("dependencies") != b.get("dependencies"):
            dependencies.append(index + 1)
    return DecisionDiff(
        actions_added=tuple(range(len(left) + 1, len(right) + 1)),
        actions_removed=tuple(range(len(right) + 1, len(left) + 1)),
        actions_changed=tuple(changed), targets_changed=tuple(targets),
        intents_changed=tuple(intents), dependencies_changed=tuple(dependencies),
    )


CRITICAL_ISSUES = frozenset({
    "OUT_OF_SCOPE_FILE", "FORBIDDEN_BEHAVIOR", "MISSING_TARGET_FILE",
    "MISSING_TARGET_SYMBOL", "INVALID_SCHEMA",
})


def repair_progress(before: Iterable[str], after: Iterable[str]) -> tuple[bool, str]:
    old, new = tuple(before), tuple(after)
    old_critical = sum(item in CRITICAL_ISSUES for item in old)
    new_critical = sum(item in CRITICAL_ISSUES for item in new)
    if new_critical < old_critical or (new_critical == old_critical and len(new) < len(old)):
        return True, "issues_reduced"
    if new_critical > old_critical:
        return False, "critical_issue_introduced"
    return False, "same_or_equivalent_issues"


def classify_failure(
    issues: Iterable[str], *, decision: Any, diff: DecisionDiff | None = None,
    shortlist_has_targets: bool = True, builder_preserved_target: bool = True,
    semantic_mirror_passed: bool = False,
) -> tuple[str, str]:
    codes = tuple(issues)
    if not shortlist_has_targets:
        return "CONTEXT", "SHORTLIST_MISSING_REQUIRED_TARGET"
    if "INVALID_SCHEMA" in codes:
        return "MODEL_OUTPUT", "SCHEMA_PARSE_FAILURE"
    if "MISSING_TARGET_SYMBOL" in codes or "MISSING_TARGET_FILE" in codes:
        return "MODEL_OUTPUT", "TARGET_OR_REQUIREMENT_UNCOVERED"
    if not builder_preserved_target:
        return "BUILDER", "BUILDER_DROPS_TARGET_INFORMATION"
    if semantic_mirror_passed and codes:
        return "VALIDATOR", "VALIDATOR_FALSE_NEGATIVE"
    if diff is not None and not diff.changed:
        return "MODEL_OUTPUT", "REPAIR_CHANGES_NOTHING"
    if diff is not None and codes:
        return "MODEL_OUTPUT", "REPAIR_IGNORES_ISSUE"
    return "MODEL_OUTPUT", "DECISION_REQUIREMENT_UNCOVERED"


@dataclass
class PlannerDecisionTelemetry:
    run_id: str
    task_train_ref: str
    split: str = "train"
    grounding_summary: dict[str, Any] = field(default_factory=dict)
    shortlist_files: list[str] = field(default_factory=list)
    shortlist_symbols: list[str] = field(default_factory=list)
    planner_raw_shape: dict[str, Any] = field(default_factory=dict)
    parsed_decision: dict[str, Any] = field(default_factory=dict)
    normalized_decision: dict[str, Any] = field(default_factory=dict)
    resolved_targets: list[str] = field(default_factory=list)
    builder_output_summary: dict[str, Any] = field(default_factory=dict)
    validation_issues_before: list[str] = field(default_factory=list)
    repair_attempted: bool = False
    repair_raw_shape: dict[str, Any] = field(default_factory=dict)
    repaired_decision: dict[str, Any] = field(default_factory=dict)
    decision_diff: dict[str, Any] = field(default_factory=dict)
    validation_issues_after: list[str] = field(default_factory=list)
    no_progress_reason: str = ""
    source: str = ""
    failure_pattern: str = ""
    final_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        _require_train(self.split)
        return asdict(self)


class PlannerTelemetryRecorder:
    def __init__(self, path: str | Path, *, split: str) -> None:
        _require_train(split)
        self.path = Path(path)
        self.split = "train"

    def append(self, telemetry: PlannerDecisionTelemetry) -> None:
        if telemetry.split != self.split:
            raise PlannerForensicsAccessDenied("telemetry split mismatch")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(telemetry.to_dict(), ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(payload + "\n")


def stable_train_ref(objective: str) -> str:
    return "train-" + hashlib.sha256(sanitize_text(objective).encode("utf-8")).hexdigest()[:16]


def aggregate_forensics(records: Iterable[dict[str, Any]], *, split: str = "train") -> dict[str, Any]:
    _require_train(split)
    rows = list(records)
    if any(str(row.get("split", "")).casefold() != "train" for row in rows):
        raise PlannerForensicsAccessDenied("non-TRAIN record in forensics input")
    patterns = Counter(str(row.get("failure_pattern") or "UNCLASSIFIED") for row in rows)
    sources = Counter(str(row.get("source") or "UNCLASSIFIED") for row in rows)
    total = len(rows)
    return {
        "schema_version": "planner-forensics/v1", "split": "train", "decisions": total,
        "repairs": sum(bool(row.get("repair_attempted")) for row in rows),
        "top_failure_patterns": dict(patterns.most_common(10)),
        "source_proportions": {
            key: round(value * 100 / total, 2) if total else 0.0 for key, value in sorted(sources.items())
        },
        "no_progress_rate": round(sum(bool(row.get("no_progress_reason")) for row in rows) * 100 / total, 2) if total else 0.0,
    }


def load_train_telemetry(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    target = Path(path)
    if not target.exists():
        return rows
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    aggregate_forensics(rows, split="train")
    return rows
