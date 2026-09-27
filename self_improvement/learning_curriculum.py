"""Auto-curriculum TRAIN-only V5.

Sélectionne les échecs à travailler de façon déterministe en combinant sévérité,
diversité de catégories et historique d'échecs. Aucune donnée validation/holdout ne
participe à la sélection.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from self_improvement.models import BenchmarkReport, ScenarioResult


@dataclass(frozen=True)
class CurriculumSelection:
    scenario_ids: list[str]
    categories: list[str]
    rationale: list[str]
    category_weights: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TrustedTrainCurriculum:
    def __init__(self, *, max_per_category: int = 4) -> None:
        self.max_per_category = max(1, min(int(max_per_category), 8))

    def select(
        self,
        report: BenchmarkReport,
        *,
        exclude_scenario_ids: Iterable[str] = (),
        maximum_cases: int = 10,
        rejected_category_counts: dict[str, int] | None = None,
    ) -> CurriculumSelection:
        excluded = set(exclude_scenario_ids)
        failures = [item for item in report.results if item.split == "train" and not item.passed and item.scenario_id not in excluded]
        if not failures:
            return CurriculumSelection([], [], ["aucun échec TRAIN disponible"], {})

        rejected_category_counts = dict(rejected_category_counts or {})
        grouped: dict[str, list[ScenarioResult]] = defaultdict(list)
        for item in failures:
            grouped[item.category or "unknown"].append(item)

        weights: dict[str, float] = {}
        for category, items in grouped.items():
            avg_deficit = sum(max(0.0, 100.0 - float(item.score)) for item in items) / max(1, len(items))
            security = sum(1 for item in items if item.security_failure)
            repeated_rejects = max(0, int(rejected_category_counts.get(category, 0)))
            # Les catégories difficiles restent prioritaires, mais une série de
            # rejets réduit légèrement leur poids pour laisser l'agent explorer
            # d'autres causes avant de retenter exactement la même zone.
            weight = 1.0 + min(1.5, avg_deficit / 60.0) + min(1.0, security * 0.25)
            weight *= max(0.55, 1.0 - min(0.45, repeated_rejects * 0.08))
            weights[category] = round(weight, 3)
            items.sort(key=lambda item: (item.score, -float(item.weight), item.scenario_id))

        # Round-robin pondéré : diversité d'abord, puis profondeur dans les catégories
        # les plus faibles. Cela évite de remplir un cycle entier avec 10 variantes du
        # même bug de surface.
        category_order = sorted(grouped, key=lambda cat: (weights[cat], len(grouped[cat])), reverse=True)
        selected: list[ScenarioResult] = []
        per_category: dict[str, int] = defaultdict(int)
        index = 0
        limit = max(1, min(int(maximum_cases), 15))
        while len(selected) < limit:
            progressed = False
            for category in category_order:
                if per_category[category] >= self.max_per_category:
                    continue
                items = grouped[category]
                if index < len(items):
                    selected.append(items[index])
                    per_category[category] += 1
                    progressed = True
                    if len(selected) >= limit:
                        break
            if not progressed:
                break
            index += 1

        rationale = [
            f"{category}: poids={weights[category]:.3f}, échecs={len(grouped[category])}, sélectionnés={per_category[category]}"
            for category in category_order if per_category[category]
        ]
        return CurriculumSelection(
            scenario_ids=[item.scenario_id for item in selected],
            categories=list(dict.fromkeys(item.category for item in selected)),
            rationale=rationale,
            category_weights=weights,
        )
