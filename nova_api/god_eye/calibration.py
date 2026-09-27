"""Leakage-safe empirical probability calibration and diagnostics."""
from __future__ import annotations

from dataclasses import dataclass
from math import log
from typing import Any
from .config import calibration_quality_tier

def probability_interval(model: CalibrationModel, raw_score: float, z: float = 1.96) -> tuple[float, float] | None:
    for low, high, probability, count in model.bins:
        if low <= raw_score <= high:
            margin = z * (probability * (1 - probability) / count) ** .5
            return max(0.0, probability-margin), min(1.0, probability+margin)
    return None

def calibration_drift(reference: CalibrationModel, recent: list[dict[str, Any]], threshold: float = .1) -> dict[str, Any]:
    pairs=[(reference.probability(float(r["raw_score"])),r.get("evaluation",{}).get("actual_direction")=="up") for r in recent if r.get("evaluation")]
    error=sum(abs(p-y) for p,y in pairs)/len(pairs) if pairs else None
    return {"sample_count":len(pairs),"error":error,"drifted":error is not None and len(pairs)>=20 and error>threshold}


@dataclass(frozen=True)
class CalibrationModel:
    model_id: str
    horizon: str
    version: str
    sample_count: int
    date_from: str
    date_to: str
    bins: tuple[tuple[float, float, float, int], ...]  # low, high, p(up), count
    quality: float

    def probability(self, raw_score: float) -> float:
        for low, high, probability, _ in self.bins:
            if low <= raw_score <= high:
                return probability
        return self.bins[0][2] if raw_score < self.bins[0][0] else self.bins[-1][2]


def chronological_split(rows: list[dict[str, Any]], fraction: float = .8) -> tuple[list[dict], list[dict]]:
    ordered = sorted(rows, key=lambda row: str(row["created_at"]))
    cut = max(1, min(len(ordered), int(len(ordered) * fraction)))
    return ordered[:cut], ordered[cut:]


def build_calibration(rows: list[dict[str, Any]], model_id: str, horizon: str, *,
                      minimum_samples: int = 20, bins: int = 5, version: str = "empirical-v1") -> CalibrationModel | None:
    eligible = [row for row in rows if row.get("model_id") == model_id and row.get("horizon") == horizon
                and row.get("evaluation") and row["evaluation"].get("actual_direction") in {"up", "down", "neutral"}]
    train, _ = chronological_split(eligible)
    if len(train) < minimum_samples:
        return None
    ordered = sorted(train, key=lambda row: float(row["raw_score"]))
    size = max(1, (len(ordered) + bins - 1) // bins)
    raw_bins = []
    for start in range(0, len(ordered), size):
        group = ordered[start:start + size]
        positives = sum(row["evaluation"]["actual_direction"] == "up" for row in group)
        raw_bins.append([float(group[0]["raw_score"]), float(group[-1]["raw_score"]), positives / len(group), len(group)])
    # Pool adjacent violations: a lightweight isotonic fit.
    index = 0
    while index < len(raw_bins) - 1:
        if raw_bins[index][2] <= raw_bins[index + 1][2]:
            index += 1; continue
        left, right = raw_bins[index], raw_bins[index + 1]
        count = left[3] + right[3]
        raw_bins[index:index + 2] = [[left[0], right[1], (left[2] * left[3] + right[2] * right[3]) / count, count]]
        index = max(0, index - 1)
    brier = sum((next(b[2] for b in raw_bins if b[0] <= float(r["raw_score"]) <= b[1]) -
                 (r["evaluation"]["actual_direction"] == "up")) ** 2 for r in train) / len(train)
    return CalibrationModel(model_id, horizon, version, len(train), str(train[0]["created_at"]),
                            str(train[-1]["created_at"]), tuple(tuple(b) for b in raw_bins), max(0.0, 1.0 - brier))


def calibration_metrics(model: CalibrationModel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    _, evaluation = chronological_split(rows)
    applicable = [r for r in evaluation if r.get("model_id") == model.model_id and r.get("horizon") == model.horizon
                  and r.get("evaluation")]
    if not applicable:
        return {"sample_count": 0, "quality_tier": "insufficient", "statistically_robust": False, "brier_score": None, "log_loss": None, "accuracy": None,
                "calibration_error": None, "reliability_bins": []}
    pairs = [(model.probability(float(r["raw_score"])), r["evaluation"]["actual_direction"] == "up") for r in applicable]
    clipped = [(min(1 - 1e-15, max(1e-15, p)), y) for p, y in pairs]
    tier = calibration_quality_tier(model.sample_count)
    return {"sample_count": len(pairs), "quality_tier": tier, "statistically_robust": tier in {"medium", "high"},
            "brier_score": sum((p - y) ** 2 for p, y in pairs) / len(pairs),
            "log_loss": -sum(y * log(p) + (1 - y) * log(1 - p) for p, y in clipped) / len(pairs),
            "accuracy": sum((p >= .5) == y for p, y in pairs) / len(pairs),
            "calibration_error": sum(abs(p - y) for p, y in pairs) / len(pairs),
            "reliability_bins": [{"low": b[0], "high": b[1], "probability_up": b[2], "count": b[3]} for b in model.bins]}
