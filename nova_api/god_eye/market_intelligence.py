"""Deterministic point-in-time market intelligence primitives (God Eyes V4)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import sqrt
from statistics import fmean, median, pstdev
from typing import Any, Iterable

from .models import as_utc

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
FEATURE_VERSION = "quant-v2.1"
PATTERN_VERSION = "pattern-v1.1"


def _available_at(row: dict[str, Any]) -> datetime:
    return as_utc(row["opened_at"]) + timedelta(seconds=TIMEFRAME_SECONDS.get(str(row.get("interval", "1m")), 0))


def point_in_time(rows: Iterable[dict[str, Any]], as_of: datetime) -> list[dict[str, Any]]:
    """Return only fully closed candles known at ``as_of``."""
    cutoff = as_utc(as_of)
    return sorted((dict(r) for r in rows if _available_at(r) <= cutoff), key=lambda r: as_utc(r["opened_at"]))


def aggregate_candles(rows: Iterable[dict[str, Any]], timeframe: str, *, as_of: datetime) -> list[dict[str, Any]]:
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("unsupported_timeframe")
    source = point_in_time(rows, as_of)
    if not source:
        return []
    seconds = TIMEFRAME_SECONDS[timeframe]
    buckets: dict[int, list[dict[str, Any]]] = {}
    for row in source:
        stamp = int(as_utc(row["opened_at"]).timestamp())
        buckets.setdefault(stamp - stamp % seconds, []).append(row)
    result = []
    for start, values in sorted(buckets.items()):
        end = datetime.fromtimestamp(start, tz=as_utc(as_of).tzinfo) + timedelta(seconds=seconds)
        if end > as_utc(as_of):
            continue
        first, last = values[0], values[-1]
        volumes = [float(v["volume"]) for v in values if v.get("volume") is not None]
        result.append({"opened_at": datetime.fromtimestamp(start, tz=as_utc(as_of).tzinfo).isoformat(),
            "interval": timeframe, "open": float(first["open"]), "high": max(float(v["high"]) for v in values),
            "low": min(float(v["low"]) for v in values), "close": float(last["close"]),
            "volume": sum(volumes) if volumes else None, "source_count": len(values),
            "provenance": {"derived_from": str(first.get("interval", "unknown")), "closed_at": end.isoformat()}})
    return result


def quantitative_features(rows: Iterable[dict[str, Any]], *, instrument: str, timeframe: str,
                          as_of: datetime, benchmark_returns: list[float] | None = None) -> dict[str, Any]:
    data = point_in_time(rows, as_of)
    closes = [float(r["close"]) for r in data]
    if len(closes) < 20:
        return {"instrument": instrument, "timeframe": timeframe, "timestamp": as_utc(as_of).isoformat(),
                "version": FEATURE_VERSION, "quality": "insufficient", "sample_count": len(closes), "values": {}}
    returns = [b / a - 1 for a, b in zip(closes, closes[1:]) if a]
    short, long = fmean(closes[-5:]), fmean(closes[-20:])
    vol = pstdev(returns[-20:]) if len(returns) > 1 else 0.0
    ranges = [(float(r["high"]) - float(r["low"])) / max(abs(float(r["close"])), 1e-12) for r in data]
    volumes = [None if r.get("volume") is None else float(r["volume"]) for r in data]
    historic_volumes = [v for v in volumes[-20:-1] if v is not None]
    relative_volume = (volumes[-1] / fmean(historic_volumes)) if volumes[-1] is not None and historic_volumes and fmean(historic_volumes) else None
    high20, low20 = max(closes[-20:]), min(closes[-20:])
    previous_high, previous_low = max(closes[-20:-1]), min(closes[-20:-1])
    deviation = (closes[-1] - long) / max(abs(long), 1e-12)
    momentum5 = closes[-1] / closes[-6] - 1
    momentum20 = closes[-1] / closes[-20] - 1
    values: dict[str, float | None] = {
        "return_1": returns[-1], "return_5": momentum5, "return_20": momentum20,
        "ma_short": short, "ma_long": long, "ma_ratio": short / long - 1 if long else 0.0,
        "slope_5": (closes[-1] - closes[-5]) / 4, "distance_ma_20": deviation,
        "trend_strength": min(1.0, abs(short / long - 1) / max(vol, .0001)),
        "momentum_short": momentum5, "momentum_medium": momentum20,
        "momentum_acceleration": momentum5 - (closes[-6] / closes[-11] - 1),
        "realized_volatility": vol, "range_proxy": fmean(ranges[-14:]),
        "volatility_ratio": (fmean(ranges[-5:]) / fmean(ranges[-20:-5])) if fmean(ranges[-20:-5]) else 0.0,
        "relative_volume": relative_volume,
        "volume_acceleration": (relative_volume - 1) if relative_volume is not None else None,
        "rolling_high": high20, "rolling_low": low20,
        "distance_resistance": high20 / closes[-1] - 1, "distance_support": closes[-1] / low20 - 1 if low20 else None,
        "breakout_strength": max(0.0, closes[-1] / previous_high - 1) / max(vol, .0001),
        "failed_breakout": float(float(data[-1]["high"]) > previous_high and closes[-1] <= previous_high),
        "range_position": (closes[-1] - low20) / max(high20 - low20, 1e-12),
        "compression": max(0.0, 1 - fmean(ranges[-5:]) / max(fmean(ranges[-20:]), 1e-12)),
        "normalized_deviation": deviation / max(vol * sqrt(20), 1e-12), "reversion_pressure": -deviation,
        "price_volume_confirmation": (1.0 if returns[-1] >= 0 else -1.0) * (relative_volume or 0.0),
    }
    if benchmark_returns and len(benchmark_returns) >= 5:
        values["relative_momentum"] = momentum5 - sum(benchmark_returns[-5:])
        paired = list(zip(returns[-20:], benchmark_returns[-20:]))
        if len(paired) > 2:
            xa, xb = [p[0] for p in paired], [p[1] for p in paired]
            ma, mb = fmean(xa), fmean(xb); denom = sqrt(sum((x-ma)**2 for x in xa) * sum((x-mb)**2 for x in xb))
            values["rolling_correlation"] = sum((a-ma)*(b-mb) for a,b in paired) / denom if denom else None
    return {"instrument": instrument, "timeframe": timeframe, "timestamp": as_utc(as_of).isoformat(),
            "version": FEATURE_VERSION, "quality": "high" if len(data) >= 60 else "standard",
            "sample_count": len(data), "values": values,
            "provenance": {"latest_candle": as_utc(data[-1]["opened_at"]).isoformat(), "point_in_time": True}}


@dataclass
class PatternEngine:
    version: str = PATTERN_VERSION

    def detect(self, features: dict[str, Any]) -> list[dict[str, Any]]:
        v = features.get("values", {})
        if not v:
            return []
        tests = {
            "momentum_continuation": (v["momentum_short"] > 0 and v["ma_ratio"] > 0, abs(v["momentum_short"]) / max(v["realized_volatility"], .0001), "momentum_short<=0"),
            "breakout": (v["breakout_strength"] > .5, v["breakout_strength"], "close<=prior_rolling_high"),
            "failed_breakout": (bool(v["failed_breakout"]), 1.0, "close>prior_rolling_high"),
            "mean_reversion_setup": (abs(v["normalized_deviation"]) > 1.5, abs(v["normalized_deviation"]) / 3, "abs(normalized_deviation)<0.5"),
            "volatility_compression": (v["compression"] > .25, v["compression"], "compression<0.1"),
            "volatility_expansion": (v["volatility_ratio"] > 1.5, v["volatility_ratio"] / 3, "volatility_ratio<1"),
            "trend_acceleration": (abs(v["momentum_acceleration"]) > max(v["realized_volatility"], .001), abs(v["momentum_acceleration"]) / max(v["realized_volatility"], .001) / 3, "momentum_acceleration changes sign"),
            "trend_exhaustion_candidate": (v["trend_strength"] > .7 and abs(v["normalized_deviation"]) > 2, abs(v["normalized_deviation"]) / 4, "normalized_deviation returns below 1"),
            "abnormal_volume": ((v["relative_volume"] or 0) > 2, (v["relative_volume"] or 0) / 4, "relative_volume<1"),
            "support_resistance_interaction": (min(v["distance_support"] or 1, v["distance_resistance"]) < .01, 1-min(v["distance_support"] or 1, v["distance_resistance"])*100, "distance_to_level>2%"),
            "regime_transition_candidate": (v["volatility_ratio"] > 1.8 or v["compression"] > .4, max(v["volatility_ratio"] / 3, v["compression"]), "volatility_ratio normalizes"),
        }
        result = []
        for kind, (matched, strength, invalidation) in tests.items():
            if matched:
                result.append({"pattern_type": kind, "instrument": features["instrument"], "timeframe": features["timeframe"],
                    "detected_at": features["timestamp"], "strength": max(0.0, min(1.0, float(strength))),
                    "quality": features["quality"], "supporting_features": {k: v[k] for k in v if k in kind or k in {"realized_volatility", "relative_volume", "ma_ratio"}},
                    "invalidation_conditions": [invalidation], "version": self.version,
                    "strength_is_probability": False})
        return result


def quantitative_divergences(rows: Iterable[dict[str, Any]], *, instrument: str, timeframe: str,
                             as_of: datetime, lookback: int = 10) -> list[dict[str, Any]]:
    """Detect objective endpoint divergence; no chart or subjective interpretation."""
    data = point_in_time(rows, as_of)
    if len(data) < max(20, lookback + 6):
        return []
    closes = [float(row["close"]) for row in data]
    volumes = [None if row.get("volume") is None else float(row["volume"]) for row in data]
    momentum = [closes[i] / closes[i-5] - 1 for i in range(5, len(closes))]
    a, b = len(closes)-1-lookback, len(closes)-1
    price_change = closes[b] / closes[a] - 1
    momentum_change = momentum[b-5] - momentum[a-5]
    result = []
    tests = []
    if price_change > 0 and momentum_change < 0: tests.append(("bearish", "price_vs_momentum", abs(price_change*momentum_change)))
    if price_change < 0 and momentum_change > 0: tests.append(("bullish", "price_vs_momentum", abs(price_change*momentum_change)))
    if volumes[a] is not None and volumes[b] is not None and volumes[a] > 0:
        volume_change = volumes[b] / volumes[a] - 1
        if price_change > 0 and volume_change < 0: tests.append(("bearish", "price_vs_volume", abs(price_change*volume_change)))
        if price_change < 0 and volume_change > 0: tests.append(("bullish", "price_vs_volume", abs(price_change*volume_change)))
    for direction, kind, magnitude in tests:
        result.append({"pattern_type": "quantitative_divergence", "divergence_type": kind,
            "direction": direction, "instrument": instrument, "timeframe": timeframe,
            "detected_at": as_utc(as_of).isoformat(), "strength": min(1.0, magnitude*100),
            "quality": "standard" if len(data) < 60 else "high",
            "supporting_points": [{"index": a, "timestamp": as_utc(data[a]["opened_at"]).isoformat(), "price": closes[a]},
                                  {"index": b, "timestamp": as_utc(data[b]["opened_at"]).isoformat(), "price": closes[b]}],
            "supporting_features": {"price_change": price_change, "momentum_change": momentum_change},
            "invalidation_conditions": ["price_or_indicator_endpoint_relationship_reverses"],
            "version": "quantitative-divergence-v1", "strength_is_probability": False})
    return result


def multi_timeframe_context(feature_sets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    states = {}
    for timeframe, feature in feature_sets.items():
        v = feature.get("values", {})
        score = float(v.get("ma_ratio", 0)) + float(v.get("momentum_short", 0))
        states[timeframe] = "bullish" if score > .002 else ("bearish" if score < -.002 else "neutral")
    directional = [v for v in states.values() if v != "neutral"]
    agreement = (max(directional.count("bullish"), directional.count("bearish")) / len(directional)) if directional else 0.0
    ordered = [states[t] for t in ("1m", "5m", "15m", "1h", "4h", "1d") if t in states]
    return {"timeframes": states, "agreement": agreement, "conflict": len(set(directional)) > 1,
            "short_term_reversal_inside_long_trend": len(ordered) >= 2 and ordered[0] != "neutral" and ordered[-1] != "neutral" and ordered[0] != ordered[-1],
            "broad_confirmation": len(directional) >= 3 and agreement >= .75, "version": "mtf-v1"}


def classify_regime_v2(features: dict[str, Any], *, event_active: bool = False) -> dict[str, Any]:
    v = features.get("values", {})
    if not v:
        return {"regime": "unknown", "confidence": 0.0, "version": "regime-v2"}
    if event_active: regime = "event_driven"
    elif v["realized_volatility"] > .04: regime = "high_volatility"
    elif v["realized_volatility"] < .002: regime = "low_volatility"
    elif v["ma_ratio"] > max(.002, v["realized_volatility"]): regime = "trending_up"
    elif v["ma_ratio"] < -max(.002, v["realized_volatility"]): regime = "trending_down"
    else: regime = "range"
    return {"regime": regime, "confidence": min(1.0, max(abs(v["ma_ratio"])/max(v["realized_volatility"],.001), .5 if event_active else 0)),
            "version": "regime-v2", "features_version": features["version"]}


def parse_microstructure(provider: str, payload: dict[str, Any], *, instrument: str, observed_at: datetime) -> dict[str, Any]:
    """Normalize compact Coinbase/Kraken level-2 payloads; never retain the full book."""
    try:
        if provider == "coinbase": bids, asks = payload["bids"], payload["asks"]
        elif provider == "kraken":
            book = next(iter(payload["result"].values())); bids, asks = book["bids"], book["asks"]
        else: raise ValueError("unsupported_provider")
        bid, ask = float(bids[0][0]), float(asks[0][0]); mid = (bid + ask) / 2
        bid_depth = sum(float(v[1]) for v in bids[:10]); ask_depth = sum(float(v[1]) for v in asks[:10])
        total = bid_depth + ask_depth
        return {"status": "available", "provider": provider, "instrument": instrument,
                "observed_at": as_utc(observed_at).isoformat(), "best_bid": bid, "best_ask": ask,
                "spread": ask-bid, "spread_bps": (ask-bid)/mid*10000 if mid else None,
                "bid_depth": bid_depth, "ask_depth": ask_depth,
                "depth_imbalance": (bid_depth-ask_depth)/total if total else 0.0, "depth_levels": min(10, len(bids), len(asks))}
    except (KeyError, IndexError, TypeError, ValueError, StopIteration, ZeroDivisionError):
        return {"status": "unavailable", "provider": provider, "instrument": instrument,
                "observed_at": as_utc(observed_at).isoformat(), "reason": "malformed_or_unsupported"}


def liquidity_metrics(snapshot: dict[str, Any], relative_volume: float | None = None) -> dict[str, Any]:
    if snapshot.get("status") != "available":
        return {"status": "unavailable", "liquidity_score": None, "expected_execution_quality_proxy": None}
    spread_score = max(0.0, 1-float(snapshot["spread_bps"])/50)
    depth_score = min(1.0, (float(snapshot["bid_depth"])+float(snapshot["ask_depth"]))/100)
    volume_score = min(1.0, max(0.0, (relative_volume or 0)/2))
    score = .5*spread_score + .35*depth_score + .15*volume_score
    return {"status": "available", "spread_bps": snapshot["spread_bps"], "depth": snapshot["bid_depth"]+snapshot["ask_depth"],
            "relative_volume": relative_volume, "liquidity_score": score,
            "expected_execution_quality_proxy": score, "is_recommendation": False, "version": "liquidity-v1"}


@dataclass
class MarketScanner:
    max_deep_analyses: int = 10
    threshold: float = .35
    cooldown_seconds: int = 300
    _last_deep: dict[str, datetime] = field(default_factory=dict)

    def scan(self, candidates: Iterable[dict[str, Any]], *, now: datetime, priority: Iterable[str] = ()) -> dict[str, Any]:
        priority_set = set(priority); scored = []
        for item in candidates:
            v = item.get("values", {})
            freshness = float(item.get("freshness", 1.0)); liquidity = float(item.get("liquidity", .5))
            interest = min(1.0, .22*min(1,abs(float(v.get("return_5",0)))*20) + .18*min(1,float(v.get("relative_volume") or 0)/3)
                + .18*min(1,float(v.get("realized_volatility",0))*20) + .12*min(1,float(v.get("volatility_ratio",0))/2)
                + .1*min(1,abs(float(v.get("momentum_short",0)))*20) + .1*liquidity + .1*freshness + .1*float(item.get("event_activity",0)))
            scored.append({"instrument": item["instrument"], "market_interest_score": interest,
                           "score_is_recommendation": False, "timestamp": as_utc(now).isoformat()})
        scored.sort(key=lambda x: (x["instrument"] not in priority_set, -x["market_interest_score"], x["instrument"]))
        selected=[]
        for value in scored:
            symbol=str(value["instrument"]); last=self._last_deep.get(symbol)
            reason="priority" if symbol in priority_set else "interest_threshold"
            if len(selected)>=self.max_deep_analyses: continue
            if symbol not in priority_set and value["market_interest_score"] < self.threshold: continue
            if last and (as_utc(now)-last).total_seconds()<self.cooldown_seconds: continue
            selected.append({**value,"reason":reason,"priority":symbol in priority_set}); self._last_deep[symbol]=as_utc(now)
        return {"scanned":len(scored),"deep_analysis_count":len(selected),"max_deep_analyses":self.max_deep_analyses,
                "stage_a":scored,"stage_b":selected,"generated_at":as_utc(now).isoformat()}


def historical_pattern_statistics(occurrences: Iterable[dict[str, Any]], *, minimum_samples: int = 20) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[float]] = {}
    for row in occurrences:
        if row.get("forward_return") is not None:
            groups.setdefault((str(row["pattern_type"]), str(row["timeframe"]), str(row["horizon"])), []).append(float(row["forward_return"]))
    result=[]
    for (pattern,timeframe,horizon), values in sorted(groups.items()):
        ordered=sorted(values); sufficient=len(values)>=minimum_samples
        result.append({"pattern_type":pattern,"timeframe":timeframe,"horizon":horizon,"occurrence_count":len(values),
            "sample_sufficient":sufficient,"mean_forward_return":fmean(values) if sufficient else None,
            "median_forward_return":median(values) if sufficient else None,"hit_rate":sum(v>0 for v in values)/len(values) if sufficient else None,
            "downside":ordered[max(0,int(len(ordered)*.1)-1)] if sufficient else None,"upside":ordered[min(len(ordered)-1,int(len(ordered)*.9))] if sufficient else None,
            "dispersion":pstdev(values) if sufficient and len(values)>1 else None})
    return result


def fuse_event_evidence(evidence: Iterable[dict[str, Any]], *, now: datetime) -> dict[str, Any]:
    """Score independent origins, retaining every evidence item and its provenance."""
    items = [dict(item) for item in evidence]
    origins: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        origin = str(item.get("origin_id") or item.get("canonical_url") or item.get("source_id") or "unknown")
        origins.setdefault(origin, []).append(item)
    qualities = [float(group[0].get("source_quality", 0)) for group in origins.values()]
    ages = [max(0.0, (as_utc(now)-as_utc(group[0].get("published_at", now))).total_seconds()) for group in origins.values()]
    contradictions = {str(i.get("stance")) for i in items if i.get("stance") in {"positive", "negative"}}
    return {"evidence":items,"evidence_count":len(items),"independent_origin_count":len(origins),
        "evidence_diversity":min(1.0,len(origins)/5),"source_quality":fmean(qualities) if qualities else 0.0,
        "freshness":fmean(max(0.0,1-age/86400) for age in ages) if ages else 0.0,
        "novelty":min(1.0,len(origins)/3),"event_importance":min(1.0,(len(origins)/5)*(fmean(qualities) if qualities else 0)),
        "contradiction_status":"contradicted" if len(contradictions)>1 else "consistent",
        "scores_are_return_probabilities":False,"version":"event-fusion-v4"}


def retrieve_similar_situations(target: dict[str, Any], candidates: Iterable[dict[str, Any]], *,
                                as_of: datetime, minimum_samples: int = 3, top_k: int = 10) -> dict[str, Any]:
    target_values=target.get("features",{}); matches=[]
    for item in candidates:
        if as_utc(item["timestamp"]) >= as_utc(as_of): continue
        values=item.get("features",{}); shared=set(target_values)&set(values)
        if not shared: continue
        distance=fmean(abs(float(target_values[k])-float(values[k]))/(1+abs(float(target_values[k]))) for k in shared
                       if target_values[k] is not None and values[k] is not None)
        score=1/(1+distance)
        if item.get("regime")==target.get("regime"): score=min(1.0,score+.1)
        matches.append({"situation_id":item.get("situation_id"),"timestamp":item["timestamp"],
                        "similarity_score":score,"subsequent_returns":item.get("subsequent_returns",{}),
                        "regime_consistent":item.get("regime")==target.get("regime")})
    matches=sorted(matches,key=lambda x:(-x["similarity_score"],str(x["timestamp"])))[:top_k]
    returns=[float(v) for m in matches for v in m["subsequent_returns"].values() if v is not None]
    sufficient=len(matches)>=minimum_samples
    return {"similar_cases":matches,"sample_count":len(matches),"sample_sufficient":sufficient,
            "distribution":{"mean":fmean(returns),"median":median(returns),"minimum":min(returns),"maximum":max(returns)} if sufficient and returns else None,
            "success_rate":sum(v>0 for v in returns)/len(returns) if sufficient and returns else None,
            "point_in_time":True,"version":"market-memory-v2"}


def signal_decay(occurrences: Iterable[dict[str, Any]], *, minimum_samples: int = 20) -> list[dict[str, Any]]:
    return [{"signal":row["pattern_type"],"timeframe":row["timeframe"],"horizon":row["horizon"],
             "sample_count":row["occurrence_count"],"mean_forward_return":row["mean_forward_return"],
             "sample_sufficient":row["sample_sufficient"]}
            for row in historical_pattern_statistics(occurrences,minimum_samples=minimum_samples)]


def intelligence_quality(*, freshness: float, integrity: float, source_diversity: float, sample_count: int,
                         calibration_quality: float, regime_confidence: float, microstructure_available: bool,
                         contradictions: bool) -> dict[str, Any]:
    parts={"freshness":freshness,"integrity":integrity,"source_diversity":source_diversity,
           "sample_size":min(1,sample_count/20),"calibration":calibration_quality,"regime":regime_confidence,
           "microstructure":1.0 if microstructure_available else .0,"contradictions":0.0 if contradictions else 1.0}
    score=sum(max(0,min(1,float(v))) for v in parts.values())/len(parts)
    reasons=[k for k,v in parts.items() if v<.5]
    return {"quality_score":score,"reasons":reasons,"components":parts,"is_probability_of_gain":False,"version":"quality-v1"}
