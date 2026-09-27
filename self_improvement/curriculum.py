"""Curriculum et saturation déterministes à partir de métriques agrégées publiques."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SaturationDiagnosis:
    saturated: bool
    duplicate_rate: float | str
    new_failure_rate: float | str
    new_root_cause_rate: float | str
    reason: str


def detect_saturation(campaigns: list[dict[str, Any]], *, window: int = 3, duplicate_threshold: float = 0.8) -> SaturationDiagnosis:
    recent = campaigns[-window:]
    if len(recent) < window:
        return SaturationDiagnosis(False, "not_available", "not_available", "not_available", "Historique insuffisant.")
    proposed = sum(int(item.get("proposed", item.get("generated", 0))) for item in recent)
    duplicates = sum(int(item.get("duplicates", 0)) for item in recent)
    failures = sum(int(item.get("new_failures", 0)) for item in recent)
    root_causes = sum(int(item.get("new_root_causes", 0)) for item in recent)
    duplicate_rate = round(duplicates / proposed, 4) if proposed else "not_available"
    new_failure_rate = round(failures / proposed, 4) if proposed else "not_available"
    root_rate = round(root_causes / proposed, 4) if proposed else "not_available"
    saturated = bool(proposed and failures == 0 and root_causes == 0 and duplicate_rate >= duplicate_threshold)
    reason = (
        "Stratégie saturée : aucun nouvel échec ni cause racine et taux de doublons élevé."
        if saturated else "Aucune saturation démontrée sur la fenêtre observée."
    )
    return SaturationDiagnosis(saturated, duplicate_rate, new_failure_rate, root_rate, reason)


def curriculum_priorities(category_history: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Renvoie des multiplicateurs bornés; ne modifie ni corpus ni holdout."""
    priorities = {}
    for category, stats in category_history.items():
        score = float(stats.get("score", 0))
        campaigns_at_100 = int(stats.get("campaigns_at_100", 0))
        failures = int(stats.get("failures", 0))
        root_causes = int(stats.get("root_causes", 0))
        generalization = stats.get("generalization_rate")
        multiplier = 1.0
        if score >= 100 and campaigns_at_100 >= 3:
            multiplier *= 0.5
        if failures >= 5 and root_causes >= 2:
            multiplier *= 1.5
        if isinstance(generalization, (int, float)) and generalization < 0.5:
            multiplier *= 1.25
        priorities[category] = round(max(0.5, min(2.0, multiplier)), 3)
    return priorities

