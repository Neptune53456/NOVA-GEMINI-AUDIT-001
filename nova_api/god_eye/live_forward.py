"""Restart-safe prospective validation with immutable model identity."""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Iterable

def _utc(value: datetime | str | None = None) -> datetime:
    if value is None: return datetime.now(timezone.utc)
    if isinstance(value, str): value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

class LiveForwardValidator:
    def __init__(self, store, *, retention_runs: int = 50) -> None:
        self.store, self.retention_runs = store, max(1, min(retention_runs, 500))

    def start(self, model_version: str, universe: Iterable[str], *, now: datetime | None = None,
              params: dict[str, Any] | None = None, validation_id: str | None = None) -> dict[str, Any]:
        started = _utc(now).isoformat(); symbols = sorted(set(universe))
        identifier = validation_id or sha256(f"{model_version}|{started}|{','.join(symbols)}".encode()).hexdigest()[:20]
        compact_params = {str(k): v for k, v in sorted((params or {}).items()) if isinstance(v, (str, int, float, bool, type(None)))}
        value = {"validation_id": identifier, "model_version": model_version, "start_at": started, "end_at": None,
                 "market_universe": symbols, "status": "running", "sample_count": 0, "performance": {},
                 "calibration": {}, "benchmark_comparison": {}, "forecast_ids": [], "params": compact_params,
                 "mode": "live_forward_paper", "historical_backtest": False}
        self.store.save_live_forward(value); return value

    def resume(self) -> dict[str, Any] | None:
        return next((run for run in self.store.live_forward_runs(self.retention_runs) if run["status"] == "running"), None)

    def update(self, validation_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        run = next((v for v in self.store.live_forward_runs(self.retention_runs) if v["validation_id"] == validation_id), None)
        if run is None: raise KeyError("live_forward_not_found")
        cutoff = _utc(now)
        ids = set(run.get("forecast_ids", [])); resolved: list[dict[str, Any]] = []
        for forecast in self.store.forecasts(limit=500):
            created, due = _utc(str(forecast["created_at"])), _utc(str(forecast["due_at"]))
            if created < _utc(str(run["start_at"])) or forecast.get("model_version") != run["model_version"]: continue
            if forecast.get("instrument") not in run["market_universe"]: continue
            ids.add(str(forecast["forecast_id"]))
            full = self.store.forecast(str(forecast["forecast_id"]))
            if due <= cutoff and full and full.get("evaluation"): resolved.append(full)
        returns = [float(v["evaluation"]["actual_return"]) for v in resolved]
        correct = [bool(v["evaluation"].get("directionally_correct")) for v in resolved]
        run.update(forecast_ids=sorted(ids), sample_count=len(resolved),
                   performance={"mean_return": sum(returns)/len(returns) if returns else None,
                                "directional_accuracy": sum(correct)/len(correct) if correct else None},
                   calibration={"resolved_samples": len(resolved)})
        self.store.save_live_forward(run); return run

    def finish(self, validation_id: str, *, now: datetime | None = None,
               benchmark_comparison: dict[str, Any] | None = None) -> dict[str, Any]:
        run = self.update(validation_id, now=now); run["status"] = "completed"; run["end_at"] = _utc(now).isoformat()
        run["benchmark_comparison"] = benchmark_comparison or {}; self.store.save_live_forward(run); return run
