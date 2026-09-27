"""Small deterministic benchmark helpers for Nova's post-V1 intelligence features.

These helpers measure behavior; they do not alter routing policy or frozen V1 evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from statistics import median
from time import perf_counter
from typing import Callable, Iterable, Any


@dataclass(frozen=True)
class ScenarioMeasurement:
    scenario_id: str
    mode: str
    success: bool
    verified: bool
    critical_failure: bool
    model_calls: int
    replans: int
    rollback_count: int
    elapsed_ms: int
    tokens_total: int | None = None

    def public(self) -> dict[str, Any]:
        return asdict(self)


class IntelligenceBenchmark:
    """Collect comparable A/B measurements without inventing unavailable metrics."""

    @staticmethod
    def summarize(rows: Iterable[ScenarioMeasurement]) -> dict[str, Any]:
        values = list(rows)
        if not values:
            return {"count": 0}
        token_values = [row.tokens_total for row in values if row.tokens_total is not None]
        elapsed = [row.elapsed_ms for row in values]
        return {
            "count": len(values),
            "success_rate": round(sum(row.success for row in values) / len(values), 4),
            "verified_rate": round(sum(row.verified for row in values) / len(values), 4),
            "critical_failures": sum(row.critical_failure for row in values),
            "model_calls_total": sum(row.model_calls for row in values),
            "replans_total": sum(row.replans for row in values),
            "rollbacks_total": sum(row.rollback_count for row in values),
            "elapsed_ms_median": int(median(elapsed)),
            "authoritative_token_coverage": round(len(token_values) / len(values), 4),
            "tokens_total": sum(token_values) if len(token_values) == len(values) else None,
        }

    def run_pair(self, *, scenario_id: str,
                 baseline: Callable[[], dict[str, Any]],
                 candidate: Callable[[], dict[str, Any]]) -> tuple[ScenarioMeasurement, ScenarioMeasurement]:
        return (
            self._run(scenario_id, "single_model", baseline),
            self._run(scenario_id, "conditional_deliberation", candidate),
        )

    @staticmethod
    def _run(scenario_id: str, mode: str, callback: Callable[[], dict[str, Any]]) -> ScenarioMeasurement:
        started = perf_counter()
        result = callback()
        elapsed_ms = int((perf_counter() - started) * 1000)
        return ScenarioMeasurement(
            scenario_id=scenario_id,
            mode=mode,
            success=bool(result.get("success")),
            verified=bool(result.get("verified")),
            critical_failure=bool(result.get("critical_failure")),
            model_calls=max(0, int(result.get("model_calls") or 0)),
            replans=max(0, int(result.get("replans") or 0)),
            rollback_count=max(0, int(result.get("rollback_count") or 0)),
            elapsed_ms=max(0, int(result.get("elapsed_ms", elapsed_ms))),
            tokens_total=(int(result["tokens_total"]) if isinstance(result.get("tokens_total"), int) else None),
        )
