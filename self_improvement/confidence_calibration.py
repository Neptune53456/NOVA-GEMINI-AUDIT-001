"""Calibration déterministe de confiance pour les sorties d'agent.

La confiance est descriptive uniquement : elle ne remplace jamais les tests, le
Judge ou le superviseur de confiance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ConfidenceEstimate:
    score: float
    band: str
    evidence: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calibrate_developer_confidence(
    *,
    tests_requested: bool,
    tests_passed: bool,
    tool_calls: int,
    iterations: int,
    reviewer_decision: str | None = None,
    reviewer_confidence: float = 0.0,
    replan_requested: bool = False,
) -> ConfidenceEstimate:
    score = 0.25
    evidence: list[str] = []
    if tests_requested and tests_passed:
        score += 0.38
        evidence.append("tests ciblés passés")
    elif tests_requested:
        score -= 0.25
        evidence.append("tests ciblés non passés")
    else:
        evidence.append("aucun test ciblé")
    if tool_calls >= 2:
        score += min(0.12, tool_calls * 0.015)
        evidence.append(f"exploration repo ({tool_calls} outils)")
    if iterations > 1:
        score -= min(0.10, (iterations - 1) * 0.04)
        evidence.append(f"{iterations} itérations nécessaires")
    if reviewer_decision == "approve" and reviewer_confidence >= 0.6:
        score += 0.10
        evidence.append("review indépendante favorable")
    elif reviewer_decision == "request_changes":
        score -= 0.22
        evidence.append("review indépendante demande des changements")
    if replan_requested:
        score = min(score, 0.25)
        evidence.append("replanification requise")
    score = round(max(0.0, min(score, 0.99)), 3)
    band = "high" if score >= 0.8 else "medium" if score >= 0.55 else "low"
    return ConfidenceEstimate(score, band, evidence)
