"""Méta-apprentissage déterministe à partir des historiques publics de l'agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
from typing import Any

from self_improvement.engineering_memory import EngineeringMemory
from self_improvement.experiment_memory import ExperimentMemory


@dataclass
class MetaLearningReport:
    experiments: int
    success_rate: float
    recurring_failure_types: dict[str, int] = field(default_factory=dict)
    strategy_success: dict[str, float] = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)
    engineering_memory_entries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MetaLearningAnalyzer:
    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.experiments = ExperimentMemory(self.repo_root / ".runtime" / "experiment_history.jsonl")
        self.engineering_memory = EngineeringMemory(self.repo_root)

    def analyze(self) -> MetaLearningReport:
        records = self.experiments.list_experiments()
        total = len(records)
        accepted = sum(1 for item in records if item.judge_decision.upper() == "ACCEPT")
        failures: dict[str, int] = {}
        strategy_total: dict[str, int] = {}
        strategy_accept: dict[str, int] = {}
        for item in records:
            failure = item.failure_type or "unknown"
            if item.judge_decision.upper() != "ACCEPT":
                failures[failure] = failures.get(failure, 0) + 1
            strategy = item.strategy or "unknown"
            strategy_total[strategy] = strategy_total.get(strategy, 0) + 1
            if item.judge_decision.upper() == "ACCEPT":
                strategy_accept[strategy] = strategy_accept.get(strategy, 0) + 1
        rates = {
            strategy: round(100.0 * strategy_accept.get(strategy, 0) / count, 1)
            for strategy, count in strategy_total.items() if count
        }
        recommendations: list[str] = []
        for failure, count in sorted(failures.items(), key=lambda item: item[1], reverse=True)[:4]:
            if count >= 2:
                recommendations.append(f"Réduire les échecs récurrents '{failure}' ({count} occurrences).")
        weak = sorted(rates.items(), key=lambda item: item[1])[:3]
        for strategy, rate in weak:
            if strategy != "unknown" and strategy_total.get(strategy, 0) >= 2 and rate < 40.0:
                recommendations.append(f"La stratégie '{strategy}' réussit peu ({rate:.1f}%) : la revoir ou la déprioriser.")
        if not recommendations:
            recommendations.append("Historique encore insuffisant pour une recommandation méta forte.")
        return MetaLearningReport(
            experiments=total,
            success_rate=round(100.0 * accepted / total, 1) if total else 0.0,
            recurring_failure_types=dict(sorted(failures.items(), key=lambda item: item[1], reverse=True)[:8]),
            strategy_success=dict(sorted(rates.items(), key=lambda item: item[1], reverse=True)),
            recommendations=recommendations[:8],
            engineering_memory_entries=int(self.engineering_memory.stats().get("entries", 0)),
        )
