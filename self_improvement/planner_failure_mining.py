"""Agrégation sûre des diagnostics Planner provenant exclusivement de TRAIN."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


class PlannerMiningAccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class PlannerTrainObservation:
    split: str
    plan_generated: bool = False
    valid_first_try: bool = False
    repair_attempted: bool = False
    repair_success: bool = False
    repair_no_progress: bool = False
    final_valid: bool = False
    issue_codes: tuple[str, ...] = ()
    model_calls: int = 0
    duration_ms: int = 0
    routing_failure: bool = False


@dataclass(frozen=True)
class PlannerTrainSummary:
    schema_version: str = "planner-train-summary/v1"
    split: str = "train"
    total: int = 0
    metrics: Mapping[str, float] = field(default_factory=dict)
    issue_counts: Mapping[str, int] = field(default_factory=dict)
    issue_percentages: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def aggregate_train_failures(observations: Iterable[PlannerTrainObservation]) -> PlannerTrainSummary:
    """Agrège sans conserver identifiant, objectif, prompt ou réponse modèle."""
    rows = list(observations)
    if any(row.split.casefold() != "train" for row in rows):
        raise PlannerMiningAccessDenied("planner failure mining accepts TRAIN only")
    total = len(rows)
    counts = Counter(code for row in rows for code in set(row.issue_codes))

    def rate(attribute: str) -> float:
        return round(100.0 * sum(bool(getattr(row, attribute)) for row in rows) / total, 2) if total else 0.0

    metrics = {
        "plan_generation_rate": rate("plan_generated"),
        "valid_first_plan_rate": rate("valid_first_try"),
        "repair_attempt_rate": rate("repair_attempted"),
        "repair_success_rate": rate("repair_success"),
        "repair_no_progress_rate": rate("repair_no_progress"),
        "final_valid_plan_rate": rate("final_valid"),
        "routing_failure_rate": rate("routing_failure"),
        "average_model_calls": round(sum(row.model_calls for row in rows) / total, 3) if total else 0.0,
        "average_planner_duration_ms": round(sum(row.duration_ms for row in rows) / total, 3) if total else 0.0,
    }
    percentages = {
        code: round(100.0 * count / total, 2) if total else 0.0
        for code, count in sorted(counts.items())
    }
    return PlannerTrainSummary(
        total=total, metrics=metrics,
        issue_counts=dict(sorted(counts.items())), issue_percentages=percentages,
    )


def save_train_summary(summary: PlannerTrainSummary, path: str | Path) -> Path:
    target = Path(path)
    if summary.split != "train" or "validation" in target.name.casefold() or "holdout" in target.name.casefold():
        raise PlannerMiningAccessDenied("only an aggregate TRAIN summary may be persisted")
    if not target.name.startswith("planner_train_") or target.suffix.casefold() != ".json":
        raise ValueError("expected benchmark_results/planner_train_*.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        json.dump(summary.to_dict(), stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    return target

