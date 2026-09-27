"""Modèles de données sérialisables du système d'auto-amélioration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Scenario:
    id: str
    category: str
    split: str
    initial_context: dict[str, Any]
    messages: list[str]
    simulated_state: dict[str, Any]
    expectations: dict[str, Any]
    success_criteria: list[dict[str, Any]]
    weight: float
    tags: list[str]
    runner: str = "interpreter"


@dataclass
class CriterionResult:
    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    critical: bool = False
    issue: str = ""


@dataclass
class ScenarioResult:
    scenario_id: str
    category: str
    split: str
    score: float
    passed: bool
    weight: float
    criteria: list[CriterionResult] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0
    tags: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def security_failure(self) -> bool:
        return any(item.critical and not item.passed for item in self.criteria)


@dataclass
class BenchmarkReport:
    dataset_version: str
    timestamp: str
    commit: str
    splits: list[str]
    score: float
    category_scores: dict[str, float]
    dimension_scores: dict[str, float]
    security_score: float
    results: list[ScenarioResult]
    duration_seconds: float
    metrics: dict[str, Any]
    tests: dict[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> list[ScenarioResult]:
        return [result for result in self.results if not result.passed]

    @property
    def current_security_failures(self) -> list[ScenarioResult]:
        """Échecs critiques présents dans ce rapport, sans notion de régression."""
        return [result for result in self.results if result.security_failure]

    def to_dict(self, *, include_results: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data["failure_count"] = len(self.failures)
        data["current_security_failure_count"] = len(self.current_security_failures)
        if not include_results:
            data.pop("results", None)
        return data
