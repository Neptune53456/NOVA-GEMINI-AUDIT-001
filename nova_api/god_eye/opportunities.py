"""Analytical opportunity ranking; never an execution or trading instruction."""
from __future__ import annotations
from hashlib import sha256
from typing import Any
from .models import as_utc, utc_now


def build_opportunity(forecast: dict[str, Any], *, downside: float | None, volatility: float,
                      data_quality: float, liquidity: float | None = None, fees: float = .001,
                      slippage: float = .001, max_uncertainty: float = .45,
                      minimum_samples: int = 20, as_of=None, max_age_seconds: int = 86400) -> dict[str, Any]:
    reasons = []
    probability = forecast.get("probability_up") if forecast.get("direction") == "up" else forecast.get("probability_down")
    expected = forecast.get("expected_return_estimate")
    uncertainty = float(forecast.get("uncertainty") or 1.0)
    if probability is None or int(forecast.get("calibration_sample_count", 0)) < minimum_samples: reasons.append("insufficient_calibration")
    if expected is None: reasons.append("expected_return_unavailable")
    if data_quality < .5: reasons.append("low_data_quality")
    if liquidity is not None and liquidity < .3: reasons.append("low_liquidity")
    if uncertainty > max_uncertainty: reasons.append("uncertainty_too_high")
    created_at = forecast.get("created_at")
    if created_at and (as_utc(as_of or utc_now()) - as_utc(created_at)).total_seconds() > max_age_seconds:
        reasons.append("stale_data")
    cost = max(0.0, fees) + max(0.0, slippage)
    net = None if expected is None or probability is None else probability * expected + (1 - probability) * (downside or 0.0) - cost
    if net is not None and net <= 0: reasons.append("expected_value_not_above_costs")
    status = "eligible" if not reasons else "rejected"
    score = None if status != "eligible" else max(0.0, min(100.0, 100 * net * data_quality * (1 - uncertainty) / max(volatility, .01)))
    identity = f'{forecast["forecast_id"]}|{fees}|{slippage}'
    return {"opportunity_id": sha256(identity.encode()).hexdigest(), "instrument": forecast["instrument"],
            "horizon": forecast["horizon"], "direction": forecast["direction"], "probability": probability,
            "expected_return": expected, "expected_value_net": net, "downside_estimate": downside,
            "uncertainty": uncertainty, "data_quality": data_quality, "opportunity_score": score,
            "calibration_sample_count": int(forecast.get("calibration_sample_count", 0)),
            "feature_snapshot_hash": forecast.get("feature_snapshot_hash"),
            "forecast_id": forecast.get("forecast_id"), "created_at": forecast.get("created_at"),
            "reasons": reasons, "status": status, "disclaimer": "analytical_ranking_only_not_buy_or_sell"}
