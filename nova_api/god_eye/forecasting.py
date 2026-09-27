"""Auditable reaction features, deterministic baselines and evaluation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import sqrt
from statistics import fmean, pstdev
from typing import Any

from .models import EventOutcome, ForecastFeatureSet, ForecastRecord, as_utc, public, utc_now

HORIZONS = {"5m": timedelta(minutes=5), "1h": timedelta(hours=1), "4h": timedelta(hours=4),
            "24h": timedelta(hours=24), "7d": timedelta(days=7)}


def reaction_features(event_id: str, instrument: str, published_at: datetime, candles: list[dict[str, Any]],
                      *, as_of: datetime | None = None) -> EventOutcome:
    as_of = as_utc(as_of or utc_now())
    ordered = sorted((c for c in candles if _available_at(c) <= as_of), key=lambda c: c["opened_at"])
    before = [c for c in ordered if _available_at(c) <= published_at]
    base = before[-1] if before else None
    reactions: dict[str, dict[str, float | None]] = {}
    if base:
        base_price = float(base["close"])
        pre = _returns(before[-20:])
        for label, delta in HORIZONS.items():
            due = published_at + delta
            eligible = [c for c in ordered if published_at < _available_at(c) <= due]
            if as_of < due or not eligible:
                continue
            end_price = float(eligible[-1]["close"])
            post = _returns([base, *eligible])
            volumes = [float(c["volume"]) for c in eligible if c.get("volume") is not None]
            pre_volumes = [float(c["volume"]) for c in before[-20:] if c.get("volume") is not None]
            path = [(float(c["close"]) / base_price) - 1 for c in eligible]
            reactions[label] = {"return": end_price / base_price - 1,
                "volatility_before": pstdev(pre) if len(pre) > 1 else 0.0,
                "volatility_after": pstdev(post) if len(post) > 1 else 0.0,
                "relative_volume": (fmean(volumes) / fmean(pre_volumes)) if volumes and pre_volumes and fmean(pre_volumes) else None,
                "max_favorable_excursion": max(path), "max_adverse_excursion": min(path)}
    outcome_id = sha256(f"{event_id}|{instrument}".encode()).hexdigest()
    return EventOutcome(outcome_id, event_id, instrument, as_of, reactions)


def build_feature_set(instrument: str, candles: list[dict[str, Any]], event: dict[str, Any],
                      analysis: dict[str, Any] | None = None, *, as_of: datetime | None = None) -> ForecastFeatureSet:
    as_of = as_utc(as_of or utc_now())
    ordered = sorted((c for c in candles if _available_at(c) <= as_of), key=lambda c: c["opened_at"])
    closes = [float(c["close"]) for c in ordered]
    if len(closes) < 2:
        raise ValueError("insufficient_market_history")
    returns = tuple(b / a - 1 for a, b in zip(closes[-21:-1], closes[-20:]) if a)
    volumes = [float(c["volume"]) for c in ordered[-20:] if c.get("volume") is not None]
    direction = str((analysis or {}).get("potential_direction", "unclear"))
    return ForecastFeatureSet(instrument, as_of, closes[-1] / closes[max(0, len(closes) - 6)] - 1,
        fmean(closes[-5:]), fmean(closes[-20:]), pstdev(returns) if len(returns) > 1 else 0.0,
        returns[-5:], (volumes[-1] / fmean(volumes[:-1])) if len(volumes) > 1 and fmean(volumes[:-1]) else None,
        str(event["event_type"]), float(event["source_quality_score"]), float(event["novelty_score"]),
        direction, float((analysis or {}).get("interpretation_confidence", 0.0)))


class BaselineForecastEngine:
    model_id = "god-eye-deterministic-baseline"
    model_version = "1"

    def forecast(self, features: ForecastFeatureSet, horizon: str) -> ForecastRecord:
        if horizon not in HORIZONS:
            raise ValueError("unsupported_horizon")
        ma_scale = max(abs(features.moving_average_long), 1e-12)
        trend = (features.moving_average_short - features.moving_average_long) / ma_scale
        event_signal = {"positive": 0.25, "negative": -0.25, "mixed": 0.0, "unclear": 0.0}.get(
            features.llm_impact_direction, 0.0)
        raw_score = max(-1.0, min(1.0, features.market_momentum * 4 + trend * 4 + event_signal))
        direction = "up" if raw_score > 0.05 else ("down" if raw_score < -0.05 else "neutral")
        snapshot = public(features)
        feature_hash = sha256(str(sorted(snapshot.items())).encode()).hexdigest()
        created = features.as_of
        forecast_id = sha256(f"{features.instrument}|{horizon}|{created.isoformat()}|{feature_hash}".encode()).hexdigest()
        return ForecastRecord(forecast_id, features.instrument, horizon, self.model_id, self.model_version,
                              feature_hash, raw_score, direction, snapshot, created, created + HORIZONS[horizon])


class EnsembleForecastEngine:
    """Transparent V4 ensemble. LLM values remain bounded input features, never probabilities."""
    model_id = "god-eye-deterministic-ensemble"
    # Keep the calibrated model lineage stable; engine_version identifies the V4 composition.
    model_version = "3"
    engine_version = "4"

    def forecast(self, features: ForecastFeatureSet, horizon: str, calibration=None) -> ForecastRecord:
        if calibration is not None and (calibration.model_id != self.model_id or calibration.horizon != horizon):
            raise ValueError("calibration_scope_mismatch")
        from .research import ForecastEnsembleV3
        baseline = BaselineForecastEngine().forecast(features, horizon)
        research = ForecastEnsembleV3(); feature_values=public(features); feature_values["baseline_score"]=baseline.raw_score
        parts=research.score(feature_values); combined=research.combine(parts,[],features.as_of,features.local_regime)
        quant = features.quantitative_features
        pattern_signal = sum((1 if p["pattern_type"] in {"breakout", "momentum_continuation", "trend_acceleration"} else
                              -1 if p["pattern_type"] in {"failed_breakout", "trend_exhaustion_candidate"} else 0)
                             * float(p["strength"]) for p in features.patterns)
        pattern_signal = max(-1.0, min(1.0, pattern_signal))
        mtf = features.multi_timeframe_context
        states = list(mtf.get("timeframes", {}).values())
        mtf_signal = ((states.count("bullish") - states.count("bearish")) / len(states)) if states else 0.0
        volume_signal = max(-1.0, min(1.0, float(quant.get("price_volume_confirmation") or 0) / 2))
        micro_signal = max(-1.0, min(1.0, float(features.microstructure.get("depth_imbalance", 0))))
        legacy = float(combined["raw_score"])
        raw = max(-1.0, min(1.0, .65*legacy + .15*pattern_signal + .12*mtf_signal + .05*volume_signal + .03*micro_signal))
        direction = "up" if raw > .05 else ("down" if raw < -.05 else "neutral")
        probability_up = calibration.probability(raw) if calibration else None
        expected = features.similar_return_median if features.similar_event_count >= 3 else None
        uncertainty = max(0.0, min(1.0, 1 - (.5 * features.similarity_confidence +
                          .5 * (calibration.quality if calibration else 0))))
        snapshot = public(features); snapshot["ensemble"]={"components":[public(p) for p in parts],**combined,
            "ensemble_version":self.engine_version,"v4_contributions":{"legacy":legacy,"patterns":pattern_signal,"multi_timeframe":mtf_signal,
                                "volume":volume_signal,"microstructure":micro_signal},"parameter_version":"phase10-v4-default"}
        feature_hash = sha256(str(sorted(snapshot.items())).encode()).hexdigest()
        forecast_id = sha256(f"{features.instrument}|{horizon}|{features.as_of.isoformat()}|{feature_hash}|v4".encode()).hexdigest()
        return ForecastRecord(forecast_id, features.instrument, horizon, self.model_id, self.model_version,
            feature_hash, raw, direction, snapshot, features.as_of, features.as_of + HORIZONS[horizon],
            probability_up=probability_up, probability_down=(None if probability_up is None else 1 - probability_up),
            calibration_quality=(calibration.quality if calibration else None),
            calibration_version=(calibration.version if calibration else None),
            calibration_sample_count=(calibration.sample_count if calibration else 0),
            expected_return_estimate=expected, uncertainty=uncertainty)


def evaluate_forecast(forecast: dict[str, Any], candles: list[dict[str, Any]], *, as_of: datetime | None = None) -> dict[str, Any] | None:
    as_of = as_utc(as_of or utc_now())
    due = as_utc(forecast["due_at"])
    if as_of < due:
        return None
    ordered = sorted(candles, key=lambda c: c["opened_at"])
    before = [c for c in ordered if _available_at(c) <= as_utc(forecast["created_at"])]
    after = [c for c in ordered if as_utc(forecast["created_at"]) < _available_at(c) <= due]
    if not before or not after:
        return None
    actual_return = float(after[-1]["close"]) / float(before[-1]["close"]) - 1
    actual_direction = "up" if actual_return > 0 else ("down" if actual_return < 0 else "neutral")
    return {"actual_return": actual_return, "actual_direction": actual_direction,
            "directionally_correct": actual_direction == forecast["direction"],
            "score_error": abs(float(forecast["raw_score"]) - actual_return),
            "evaluated_at": as_of.isoformat()}


def _returns(candles: list[dict[str, Any]]) -> list[float]:
    closes = [float(c["close"]) for c in candles]
    return [b / a - 1 for a, b in zip(closes, closes[1:]) if a]


def _available_at(candle: dict[str, Any]) -> datetime:
    duration = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5),
                "1h": timedelta(hours=1), "1d": timedelta(days=1)}.get(str(candle.get("interval")), timedelta(0))
    return as_utc(candle["opened_at"]) + duration
