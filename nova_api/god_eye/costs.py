"""Versioned deterministic execution-cost and price estimates."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class VenueCost:
    maker_bps: float
    taker_bps: float
    fx_bps: float = 0.0


@dataclass(frozen=True)
class CostConfig:
    version: str = "cost-v1"
    venue_costs: dict[str, VenueCost] = field(default_factory=lambda: {
        "coinbase": VenueCost(4.0, 8.0), "kraken": VenueCost(3.0, 7.0)})
    asset_defaults_bps: dict[str, float] = field(default_factory=lambda: {
        "crypto": 10.0, "equity": 5.0, "etf": 4.0, "index": 4.0,
        "fx": 2.0, "commodity": 7.0})
    fallback_spread_bps: float = 12.0
    fallback_slippage_bps: float = 8.0
    unknown_policy: str = "degrade"


class CostEngine:
    def __init__(self, config: CostConfig | None = None) -> None:
        self.config = config or CostConfig()

    def estimate(self, *, asset_class: str, venue: str | None, order_style: str = "taker",
                 size: float, price: float, microstructure: dict[str, Any] | None = None,
                 conversion_required: bool = False) -> dict[str, Any]:
        if size <= 0 or price <= 0:
            raise ValueError("size_and_price_must_be_positive")
        venue_key = (venue or "").lower(); venue_cost = self.config.venue_costs.get(venue_key)
        known = venue_cost is not None or asset_class in self.config.asset_defaults_bps
        fee_bps = ((venue_cost.maker_bps if order_style == "maker" else venue_cost.taker_bps)
                   if venue_cost else self.config.asset_defaults_bps.get(asset_class, 15.0))
        micro = microstructure or {}; spread_bps = (float(micro["spread_bps"])
            if micro.get("status") == "available" and micro.get("spread_bps") is not None
            else self.config.fallback_spread_bps)
        depth_notional = 0.0
        if micro.get("status") == "available":
            depth_notional = (float(micro.get("bid_depth", 0)) + float(micro.get("ask_depth", 0))) * price
        size_ratio = size / max(depth_notional, size * 10) if depth_notional else min(1.0, size / 100_000)
        slippage_bps = self.config.fallback_slippage_bps * (1 + min(4.0, size_ratio * 10))
        fx_bps = venue_cost.fx_bps if conversion_required and venue_cost else (5.0 if conversion_required else 0.0)
        entry_fee = fee_bps / 10_000; exit_fee = fee_bps / 10_000
        spread = spread_bps / 10_000; slippage = slippage_bps / 10_000; fx = fx_bps / 10_000
        total = entry_fee + exit_fee + spread + 2 * slippage + fx
        uncertainty = min(1.0, .15 + (.25 if micro.get("status") != "available" else 0) + (0 if known else .4))
        return {"entry_fee": entry_fee, "exit_fee": exit_fee, "spread": spread,
            "expected_slippage": slippage, "fx_conversion_cost": fx, "total_round_trip_cost": total,
            "cost_uncertainty": uncertainty, "known": known, "quality": "high" if known and uncertainty <= .25 else "degraded",
            "venue": venue, "asset_class": asset_class, "order_style": order_style, "notional": size,
            "fee_bps": fee_bps, "spread_bps": spread_bps, "slippage_bps_each_side": slippage_bps,
            "config_version": self.config.version, "config": {"unknown_policy": self.config.unknown_policy}}

    def execution_prices(self, reference_price: float, direction: str, cost: dict[str, Any]) -> dict[str, float]:
        side = 1 if direction in {"up", "long", "bullish"} else -1
        half_spread = float(cost["spread"]) / 2; slip = float(cost["expected_slippage"])
        return {"reference_price": reference_price,
            "realistic_entry_price": reference_price * (1 + side * (half_spread + slip)),
            "realistic_exit_price": reference_price * (1 - side * (half_spread + slip)),
            "spread_impact": reference_price * half_spread,
            "slippage_estimate": reference_price * slip,
            "model_version": self.config.version}

    def public_config(self) -> dict[str, Any]:
        return asdict(self.config)

