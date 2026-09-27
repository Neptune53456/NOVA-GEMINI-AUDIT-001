"""Deterministic, past-only similar-event retrieval."""
from __future__ import annotations
from datetime import datetime
from statistics import fmean, median
from typing import Any
from .models import as_utc


def retrieve_similar(target: dict[str, Any], candidates: list[dict[str, Any]], outcomes: dict[str, list[dict]],
                     *, horizon: str, as_of: datetime, top_k: int = 5) -> list[dict[str, Any]]:
    target_entities = set(target.get("entities", []))
    result = []
    for event in candidates:
        if event.get("event_id") == target.get("event_id") or as_utc(event["published_at"]) >= as_utc(as_of):
            continue
        score = .35 * (event.get("event_type") == target.get("event_type"))
        score += .25 * bool(target_entities & set(event.get("entities", [])))
        score += .15 * (1 - min(1.0, abs(float(event.get("source_quality_score", 0)) - float(target.get("source_quality_score", 0)))))
        score += .15 * (1 - min(1.0, abs(float(event.get("novelty_score", 0)) - float(target.get("novelty_score", 0)))))
        score += .10 * (event.get("market_regime", "unknown") == target.get("market_regime", "unknown"))
        reactions = [o["reactions"][horizon] for o in outcomes.get(str(event["event_id"]), []) if horizon in o.get("reactions", {})]
        if reactions:
            result.append({"event_id": event["event_id"], "similarity_score": round(score, 6),
                           "observed_reactions": reactions, "source_refs": event.get("evidence_refs", [])})
    return sorted(result, key=lambda x: (-x["similarity_score"], str(x["event_id"])))[:top_k]


def similar_features(matches: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(r["return"]) for m in matches for r in m["observed_reactions"] if r.get("return") is not None]
    if not returns:
        return {"count": 0, "mean_return": None, "median_return": None, "positive_ratio": None,
                "downside": None, "confidence": 0.0}
    ordered = sorted(returns)
    return {"count": len(matches), "mean_return": fmean(returns), "median_return": median(returns),
            "positive_ratio": sum(v > 0 for v in returns) / len(returns),
            "downside": ordered[max(0, int(len(ordered) * .1) - 1)],
            "confidence": min(1.0, len(matches) / 5) * fmean(m["similarity_score"] for m in matches)}
