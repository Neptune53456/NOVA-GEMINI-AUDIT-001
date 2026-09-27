"""Deterministic market regime classification."""
from __future__ import annotations
from statistics import fmean, pstdev
from typing import Any


def classify_regime(candles: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(candles, key=lambda c: str(c["opened_at"]))
    closes = [float(c["close"]) for c in ordered[-60:]]
    if len(closes) < 10:
        return {"regime": "unknown", "confidence": 0.0, "features": {}}
    returns = [b / a - 1 for a, b in zip(closes, closes[1:]) if a]
    volatility = pstdev(returns) if len(returns) > 1 else 0.0
    short, long = fmean(closes[-5:]), fmean(closes[-20:])
    trend = (short - long) / max(abs(long), 1e-12)
    threshold = max(.002, volatility)
    if volatility >= .04: regime = "high_volatility"
    elif trend > threshold: regime = "trending_up"
    elif trend < -threshold: regime = "trending_down"
    elif volatility <= .002: regime = "low_volatility"
    else: regime = "range"
    return {"regime": regime, "confidence": min(1.0, abs(trend) / max(threshold, 1e-12)),
            "features": {"return": closes[-1] / closes[0] - 1, "realized_volatility": volatility,
                         "ma_trend": trend}}


def global_regime(series: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    values = [classify_regime(c) for c in series.values()]
    known = [v for v in values if v["regime"] != "unknown"]
    if not known: return {"regime": "unknown", "confidence": 0.0}
    positive = sum(v["features"].get("return", 0) > 0 for v in known) / len(known)
    volatility = fmean(v["features"].get("realized_volatility", 0) for v in known)
    return {"regime": "risk_on" if positive >= .65 else ("risk_off" if positive <= .35 else
            ("high_volatility" if volatility >= .04 else "range")), "confidence": abs(positive - .5) * 2}
