"""Budgets adaptatifs déterministes V5.

La politique peut réduire ou répartir un budget en fonction du plan, mais ne peut
jamais dépasser les plafonds durs fournis par le control-plane.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from self_improvement.campaign_manager import (
    CampaignBudget, CampaignTask, required_campaign_seconds, required_task_seconds,
)
from self_improvement.engineering_planner import EngineeringPlan


@dataclass(frozen=True)
class BudgetDecision:
    budget: CampaignBudget
    complexity: float
    rationale: str
    structural_floor_seconds: float = 0.0
    adaptive_target_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AdaptiveBudgetPolicy:
    """Alloue davantage d'essais aux plans complexes sans élargir les hard caps."""

    def __init__(
        self, hard_cap: CampaignBudget | None = None, *,
        task_duration: Callable[[CampaignTask], float] = required_task_seconds,
    ) -> None:
        self.hard_cap = hard_cap or CampaignBudget()
        self.task_duration = task_duration

    def decide(self, plan: EngineeringPlan, *, requested: CampaignBudget | None = None) -> BudgetDecision:
        hard = requested or self.hard_cap
        tasks = list(plan.tasks)
        if not tasks:
            return BudgetDecision(hard, 0.0, "plan vide; budget inchangé")
        avg_risk = sum(float(item.estimated_risk) for item in tasks) / len(tasks)
        avg_cost = sum(float(item.estimated_cost) for item in tasks) / len(tasks)
        new_files = sum(len(list(item.metadata.get("new_files", []) or [])) for item in tasks)
        dependencies = sum(len(item.dependencies) for item in tasks)
        complexity = min(10.0, len(tasks) * 0.9 + avg_risk * 0.35 + avg_cost * 0.25 + new_files * 0.4 + dependencies * 0.15)

        # Les noms exacts de CampaignBudget sont découverts via son dataclass;
        # on part toujours des hard caps et on ne fait que diminuer.
        values = asdict(hard)
        if "max_tasks" in values:
            values["max_tasks"] = min(int(values["max_tasks"]), max(len(tasks), min(int(values["max_tasks"]), len(tasks) + 2)))
        if "max_total_attempts" in values:
            desired = max(len(tasks), int(round(len(tasks) * (1.2 + complexity / 20.0))))
            values["max_total_attempts"] = min(int(values["max_total_attempts"]), desired)
        if "max_model_calls" in values:
            # Exploration, plan court, generation, correction et review peuvent
            # toutes etre necessaires pour une seule tache de production.
            desired_calls = max(len(tasks) * 8, int(round(len(tasks) * (2.0 + complexity / 4.0))))
            values["max_model_calls"] = min(int(values["max_model_calls"]), desired_calls)
        if "max_duration_seconds" in values:
            desired_seconds = max(60.0, 45.0 * len(tasks) + complexity * 15.0)
            structural_floor = required_campaign_seconds(
                [self.task_duration(task) for task in tasks], CampaignBudget(**values),
            )
            values["max_duration_seconds"] = min(
                float(values["max_duration_seconds"]), max(desired_seconds, structural_floor),
            )
        budget = CampaignBudget(**values)
        return BudgetDecision(
            budget=budget,
            complexity=round(complexity, 2),
            structural_floor_seconds=structural_floor,
            adaptive_target_seconds=desired_seconds,
            rationale=f"{len(tasks)} tâches, risque moyen {avg_risk:.1f}, coût moyen {avg_cost:.1f}, {new_files} nouveaux fichiers",
        )
