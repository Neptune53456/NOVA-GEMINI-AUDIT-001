"""NOVA Star Finder: deterministic, analytical and paper-only opportunity decisions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from statistics import median
from typing import Any, Iterable

from .models import as_utc, utc_now


@dataclass(frozen=True)
class StarFinderConfig:
    version: str = "star-finder-v1"
    score_version: str = "star-score-v1"
    minimum_net_return: float = .001
    minimum_samples: int = 20
    minimum_liquidity: float = .30
    maximum_uncertainty: float = .45
    maximum_spread_bps: float = 100.0
    maximum_cost_to_edge: float = .75
    require_calibration: bool = True
    maximum_age_seconds: int = 86_400
    small_capital: bool = False
    capital: float = 10_000.0
    minimum_notional: float = 25.0


def expected_net_return(forecast: dict[str, Any], cost: dict[str, Any], *, historical_returns: Iterable[float] = ()) -> dict[str, Any]:
    values = sorted(float(v) for v in historical_returns)
    gross = forecast.get("expected_return_estimate")
    samples = int(forecast.get("calibration_sample_count", 0))
    probability = (forecast.get("probability_up") if forecast.get("direction") == "up"
                   else forecast.get("probability_down"))
    if gross is None:
        return {"expected_gross_return": None, "expected_costs": cost.get("total_round_trip_cost"),
            "expected_net_return": None, "median_return": None, "downside_quantile": None,
            "upside_quantile": None, "calibrated_probability": probability, "uncertainty": 1.0,
            "sample_count": samples, "reason": "insufficient_return_data", "version": "expected-net-v1"}
    total = float(cost["total_round_trip_cost"])
    return {"expected_gross_return": float(gross), "expected_costs": total,
        "expected_net_return": float(gross) - total,
        "median_return": median(values) if values else forecast.get("similar_return_median"),
        "downside_quantile": values[max(0, int(len(values)*.1)-1)] if values else forecast.get("similar_downside"),
        "upside_quantile": values[min(len(values)-1, int(len(values)*.9))] if values else None,
        "calibrated_probability": probability, "uncertainty": float(forecast.get("uncertainty") or 1.0),
        "sample_count": samples, "version": "expected-net-v1"}


def rejection_gates(opportunity: dict[str, Any], config: StarFinderConfig | None = None) -> list[str]:
    c = config or StarFinderConfig(); reasons = []
    if opportunity.get("stale"): reasons.append("stale_data")
    if float(opportunity.get("intelligence_quality", 0)) < .5: reasons.append("poor_data_integrity")
    if c.require_calibration and opportunity.get("calibrated_probability") is None: reasons.append("calibration_required")
    if int(opportunity.get("sample_count", 0)) < c.minimum_samples: reasons.append("insufficient_sample")
    if opportunity.get("expected_net_return") is None or float(opportunity["expected_net_return"]) <= c.minimum_net_return: reasons.append("expected_net_return_below_threshold")
    if float(opportunity.get("liquidity_quality", 0)) < c.minimum_liquidity: reasons.append("insufficient_liquidity")
    if float(opportunity.get("uncertainty", 1)) > c.maximum_uncertainty: reasons.append("excessive_uncertainty")
    cost = opportunity.get("cost_estimate", {})
    if not cost.get("known", False) and cost.get("config", {}).get("unknown_policy") == "reject": reasons.append("unknown_cost")
    if not cost.get("known", False) and cost.get("config", {}).get("unknown_policy") == "degrade":
        uncertainty = float(cost.get("cost_uncertainty", 1))
        net = float(opportunity.get("expected_net_return") or 0)
        required_margin = c.minimum_net_return * (1 + 2 * uncertainty)
        if net <= required_margin: reasons.append("uncertain_cost_margin_insufficient")
    if float(cost.get("spread_bps", 0)) > c.maximum_spread_bps: reasons.append("excessive_spread")
    gross = opportunity.get("expected_gross_return")
    if gross and float(cost.get("total_round_trip_cost", 0))/abs(float(gross)) > c.maximum_cost_to_edge: reasons.append("costs_too_high")
    if opportunity.get("contradiction_status") == "contradicted": reasons.append("strong_contradiction")
    if opportunity.get("provider_degraded"): reasons.append("provider_degraded")
    if opportunity.get("risk_approved") is False: reasons.append("risk_engine_rejected")
    if c.small_capital:
        notional = min(c.capital * .1, float(cost.get("notional", 0)))
        if notional < c.minimum_notional: reasons.append("small_capital_minimum_notional")
    return sorted(set(reasons))


def star_score(opportunity: dict[str, Any]) -> dict[str, Any]:
    net = max(-.1, min(.1, float(opportunity.get("expected_net_return") or 0)))
    downside = abs(min(0.0, float(opportunity.get("downside") or 0)))
    uncertainty = max(0.0, min(1.0, float(opportunity.get("uncertainty", 1))))
    probability = opportunity.get("calibrated_probability")
    components = {"net_edge": max(0.0, min(1.0, net/.05)), "downside_control": max(0.0, 1-downside/.1),
        "certainty": 1-uncertainty, "calibration": max(0.0, min(1.0, float(probability))) if probability is not None else 0.0,
        "liquidity": max(0.0, min(1.0, float(opportunity.get("liquidity_quality", 0)))),
        "intelligence": max(0.0, min(1.0, float(opportunity.get("intelligence_quality", 0)))),
        "regime_stability": max(0.0, min(1.0, float(opportunity.get("regime_stability", .5)))),
        "evidence_diversity": max(0.0, min(1.0, float(opportunity.get("evidence_diversity", 0)))),
        "sample_size": min(1.0, int(opportunity.get("sample_count", 0))/100),
        "cost_efficiency": max(0.0, 1-float(opportunity.get("cost_estimate", {}).get("total_round_trip_cost", 1))/.03)}
    weights = {"net_edge":.25,"downside_control":.12,"certainty":.12,"calibration":.12,"liquidity":.1,
        "intelligence":.1,"regime_stability":.06,"evidence_diversity":.05,"sample_size":.04,"cost_efficiency":.04}
    return {"score": round(100*sum(components[k]*weights[k] for k in weights), 4), "components": components,
        "weights": weights, "version": "star-score-v1", "is_probability_of_gain": False}


def entry_timing(opportunity: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = as_utc(now or utc_now()); reasons = list(opportunity.get("rejection_reasons", []))
    if reasons: action = "IGNORE"
    elif not opportunity.get("entry_confirmed", False): action = "WAIT"
    else: action = "ENTER"
    price = float(opportunity.get("expected_entry") or opportunity.get("reference_price") or 0)
    width = price * max(.001, float(opportunity.get("uncertainty", .1))*.02)
    return {"action": action, "reasons": reasons or (["entry_confirmation_pending"] if action == "WAIT" else ["qualified_and_confirmed"]),
        "desired_entry_zone": [price-width, price+width] if price else None,
        "trigger_conditions": ["fresh_data", "risk_engine_ok", "minimum_confirmation"],
        "invalidation_conditions": list(opportunity.get("invalidation", [])),
        "expires_at": (now+timedelta(hours=24)).isoformat(), "version": "entry-timing-v1"}


def position_decision(position: dict[str, Any], opportunity: dict[str, Any] | None, *, risk_approved: bool = True) -> dict[str, Any]:
    evidence = [str(v) for v in (opportunity or {}).get("supporting_events", [])]
    if not risk_approved or opportunity is None: action, reasons = "EXIT", ["risk_limit" if not risk_approved else "thesis_unavailable"]
    elif opportunity.get("thesis_invalidated"): action, reasons = "EXIT", ["thesis_invalidated"]
    elif float(opportunity.get("expected_net_return") or -1) <= 0: action, reasons = "EXIT", ["remaining_value_below_threshold"]
    elif float(opportunity.get("uncertainty", 1)) > .6: action, reasons = "REDUCE", ["uncertainty_increased"]
    elif opportunity.get("new_independent_evidence") and float(opportunity.get("expected_net_return", 0)) > .01: action, reasons = "ADD", ["thesis_strengthened"]
    else: action, reasons = "HOLD", ["thesis_intact"]
    return {"position_id": position.get("position_id"), "action": action, "reasons": reasons,
        "evidence_refs": evidence, "paper_only": True, "version": "position-decision-v1"}


def rank_opportunities(items: Iterable[dict[str, Any]], *, limit: int = 100) -> dict[str, Any]:
    qualified = [dict(v) for v in items if v.get("status") == "QUALIFIED"]
    qualified.sort(key=lambda v: (-float(v.get("star_score", 0)), -float(v.get("expected_net_return", 0)), str(v.get("opportunity_id"))))
    ranked = [{**v, "rank": index+1} for index, v in enumerate(qualified[:limit])]
    by_asset = {}; by_horizon = {}; by_direction = {}
    for value in ranked:
        by_asset.setdefault(str(value.get("asset_class", "unknown")), []).append(value)
        by_horizon.setdefault(str(value.get("horizon", "unknown")), []).append(value)
        by_direction.setdefault(str(value.get("direction", "unknown")), []).append(value)
    return {"status": "OK" if ranked else "NO QUALIFIED OPPORTUNITY", "opportunities": ranked,
        "by_asset_class": by_asset, "by_horizon": by_horizon, "by_direction": by_direction,
        "generated_at": utc_now().isoformat(), "version": "global-ranking-v1"}


def lifecycle_transition(opportunity_id: str, previous: str | None, target: str, *, reasons: list[str], at: datetime | None = None,
                         evidence_refs: list[str] | None = None, model_version: str | None = None,
                         strategy_version: str | None = None, portfolio_ref: str | None = None,
                         trade_ref: str | None = None) -> dict[str, Any]:
    allowed = {None:{"DETECTED"}, "DETECTED":{"WATCHING","QUALIFIED","REJECTED","EXPIRED"},
        "WATCHING":{"QUALIFIED","REJECTED","EXPIRED"}, "QUALIFIED":{"ENTER","REJECTED","EXPIRED"},
        "ENTER":{"OPEN"}, "OPEN":{"HOLD","ADD","REDUCE","EXIT"}, "HOLD":{"HOLD","ADD","REDUCE","EXIT"},
        "ADD":{"HOLD","ADD","REDUCE","EXIT"}, "REDUCE":{"HOLD","ADD","REDUCE","EXIT"},
        "EXIT":{"CLOSED"}, "CLOSED":{"RESOLVED"}}
    if target not in allowed.get(previous, set()): raise ValueError("invalid_lifecycle_transition")
    at = as_utc(at or utc_now()); key = sha256(f"{opportunity_id}|{previous}|{target}|{at.isoformat()}".encode()).hexdigest()
    return {"transition_id": key, "opportunity_id": opportunity_id, "from_status": previous,
        "to_status": target, "timestamp": at.isoformat(), "reasons": reasons,
        "evidence_refs": evidence_refs or [], "model_version": model_version,
        "strategy_version": strategy_version, "portfolio_ref": portfolio_ref, "trade_ref": trade_ref,
        "version": "opportunity-lifecycle-v2"}
