"""Deterministic paper-only portfolio and chronological evaluation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from math import sqrt
import random
from typing import Iterable

from .models import as_utc, utc_now


@dataclass(frozen=True)
class PaperConfig:
    initial_cash: float = 100_000.0
    fee_bps: float = 5.0
    slippage_bps: float = 5.0
    max_gross_exposure: float = .75
    max_position_fraction: float = .10
    max_positions: int = 10
    min_net_ev: float = .001
    max_uncertainty: float = .45
    min_data_quality: float = .6
    strategy_version: str = "god-eyes-paper-v1"


@dataclass
class PaperPosition:
    position_id: str
    instrument: str
    side: str
    quantity: float
    entry_price: float
    entry_at: datetime
    notional: float
    opportunity_id: str
    strategy_version: str
    feature_snapshot_ref: str
    fees: float
    horizon: str
    unrealized_pnl: float = 0.0


@dataclass
class PaperTrade:
    trade_id: str
    position_id: str
    instrument: str
    side: str
    quantity: float
    entry_price: float
    exit_price: float
    entry_at: datetime
    exit_at: datetime
    realized_pnl: float
    fees: float
    slippage: float
    reason: str
    opportunity_id: str
    strategy_version: str


class PaperPortfolio:
    """Accounting ledger with no broker/network capability."""
    def __init__(self, config: PaperConfig | None = None) -> None:
        self.config = config or PaperConfig(); self.cash = self.config.initial_cash
        self.positions: dict[str, PaperPosition] = {}; self.trades: list[PaperTrade] = []
        self.equity_curve: list[dict[str, float | str]] = []

    @property
    def realized_pnl(self) -> float: return sum(t.realized_pnl for t in self.trades)

    def equity(self, prices: dict[str, float] | None = None) -> float:
        prices = prices or {}
        return self.cash + sum(p.quantity * prices.get(p.instrument, p.entry_price) for p in self.positions.values())

    def open(self, opportunity: dict[str, object], price: float, at: datetime | str,
             *, fraction: float | None = None) -> dict[str, object]:
        at = as_utc(at); oid = str(opportunity.get("opportunity_id", "")); symbol = str(opportunity.get("instrument", ""))
        reasons = PaperStrategy(self.config).reject_reasons(opportunity)
        if oid and any(p.opportunity_id == oid for p in self.positions.values()) or any(t.opportunity_id == oid for t in self.trades):
            reasons.append("duplicate_opportunity")
        if len(self.positions) >= self.config.max_positions: reasons.append("position_limit")
        equity = self.equity(); target = equity * min(fraction or self.config.max_position_fraction, self.config.max_position_fraction)
        exposure = sum(p.notional for p in self.positions.values())
        if exposure + target > equity * self.config.max_gross_exposure: reasons.append("exposure_limit")
        if price <= 0: reasons.append("invalid_price")
        if reasons: return {"status": "rejected", "reasons": sorted(set(reasons)), "opportunity_id": oid}
        side = "long" if str(opportunity.get("direction", "up")) == "up" else "short"
        slippage = price * self.config.slippage_bps / 10_000 * (1 if side == "long" else -1)
        fill = price + slippage; fee = target * self.config.fee_bps / 10_000
        quantity = target / fill * (1 if side == "long" else -1)
        if side == "long" and target + fee > self.cash: return {"status": "rejected", "reasons": ["insufficient_cash"], "opportunity_id": oid}
        self.cash -= quantity * fill + fee
        pid = sha256(f"{oid}|{at.isoformat()}".encode()).hexdigest()
        snapshot = str(opportunity.get("feature_snapshot_hash") or opportunity.get("forecast_id") or oid)
        self.positions[pid] = PaperPosition(pid, symbol, side, quantity, fill, at, abs(quantity * fill), oid,
            self.config.strategy_version, snapshot, fee, str(opportunity.get("horizon", "24h")))
        self.snapshot(at, {symbol: price})
        return {"status": "opened", "position": asdict(self.positions[pid]), "reason": "eligible_policy"}

    def close(self, position_id: str, price: float, at: datetime | str, reason: str = "horizon_expired") -> PaperTrade | None:
        position = self.positions.pop(position_id, None)
        if not position: return None
        at = as_utc(at); direction = 1 if position.quantity > 0 else -1
        slip = price * self.config.slippage_bps / 10_000 * direction
        fill = price - slip; exit_value = position.quantity * fill
        fee = abs(exit_value) * self.config.fee_bps / 10_000
        self.cash += exit_value - fee
        pnl = position.quantity * (fill - position.entry_price) - position.fees - fee
        trade = PaperTrade(sha256(f"{position_id}|{at.isoformat()}".encode()).hexdigest(), position_id,
            position.instrument, position.side, position.quantity, position.entry_price, fill, position.entry_at,
            at, pnl, position.fees + fee, abs(position.quantity * slip), reason, position.opportunity_id,
            position.strategy_version)
        self.trades.append(trade); self.snapshot(at, {position.instrument: price}); return trade

    def snapshot(self, at: datetime | str, prices: dict[str, float]) -> dict[str, float | str]:
        for p in self.positions.values():
            p.unrealized_pnl = p.quantity * (prices.get(p.instrument, p.entry_price) - p.entry_price) - p.fees
        value = {"timestamp": as_utc(at).isoformat(), "equity": self.equity(prices), "cash": self.cash,
                 "gross_exposure": sum(abs(p.quantity * prices.get(p.instrument, p.entry_price)) for p in self.positions.values())}
        self.equity_curve.append(value); return value

    def public(self) -> dict[str, object]:
        return {"mode": "paper", "cash": self.cash, "positions": [asdict(p) for p in self.positions.values()],
                "realized_pnl": self.realized_pnl, "unrealized_pnl": sum(p.unrealized_pnl for p in self.positions.values()),
                "equity_curve": self.equity_curve, "config": asdict(self.config)}


class PaperStrategy:
    def __init__(self, config: PaperConfig | None = None) -> None: self.config = config or PaperConfig()
    def reject_reasons(self, opportunity: dict[str, object]) -> list[str]:
        reasons = []
        if opportunity.get("status") not in {"accepted", "eligible"}: reasons.append("opportunity_not_accepted")
        if int(opportunity.get("calibration_sample_count", 0)) < 20: reasons.append("calibration_unavailable")
        ev = opportunity.get("expected_value_net", opportunity.get("net_expected_value", opportunity.get("net_ev", opportunity.get("expected_value"))))
        if ev is None or float(ev) <= self.config.min_net_ev: reasons.append("net_ev_below_threshold")
        if float(opportunity.get("uncertainty", 1.0)) > self.config.max_uncertainty: reasons.append("uncertainty_too_high")
        if float(opportunity.get("data_quality", 0.0)) < self.config.min_data_quality: reasons.append("data_quality_too_low")
        return reasons


def portfolio_metrics(portfolio: PaperPortfolio) -> dict[str, object]:
    curve = portfolio.equity_curve; trades = portfolio.trades; initial = portfolio.config.initial_cash
    final = float(curve[-1]["equity"]) if curve else portfolio.equity(); total = final / initial - 1
    wins = [t.realized_pnl for t in trades if t.realized_pnl > 0]; losses = [t.realized_pnl for t in trades if t.realized_pnl < 0]
    values = [float(v["equity"]) for v in curve]; peak = initial; drawdown = 0.0
    for value in values: peak = max(peak, value); drawdown = min(drawdown, value / peak - 1)
    returns = [values[i] / values[i-1] - 1 for i in range(1, len(values)) if values[i-1]]
    duration = (as_utc(curve[-1]["timestamp"]) - as_utc(curve[0]["timestamp"])).days if len(curve) > 1 else 0
    vol = (sqrt(252) * (sum((r-sum(returns)/len(returns))**2 for r in returns)/(len(returns)-1))**.5) if len(returns) >= 20 else None
    return {"total_return": total, "annualized_return": (1+total)**(365/duration)-1 if duration >= 365 and total > -1 else None,
        "trade_count": len(trades), "hit_rate": len(wins)/len(trades) if trades else None,
        "average_win": sum(wins)/len(wins) if wins else None, "average_loss": sum(losses)/len(losses) if losses else None,
        "profit_factor": sum(wins)/abs(sum(losses)) if losses else None, "max_drawdown": drawdown,
        "volatility": vol, "sharpe": (sum(returns)/len(returns))/ (vol/sqrt(252))*sqrt(252) if vol and len(returns)>=30 else None,
        "turnover": sum(t.quantity*t.entry_price for t in trades)/initial, "fees": sum(t.fees for t in trades),
        "slippage": sum(t.slippage for t in trades), "metrics_reliable": len(trades) >= 20}


def benchmark_comparison(prices: list[tuple[datetime, float]], strategy_return: float, seed: int = 0) -> dict[str, object]:
    if len(prices) < 2: return {"no_trade": 0.0, "buy_and_hold": None, "momentum": None, "random_direction": None}
    ordered = sorted(prices, key=lambda row: row[0]); returns = [ordered[i][1]/ordered[i-1][1]-1 for i in range(1,len(ordered))]
    momentum = 1.0
    for i in range(1, len(returns)): momentum *= 1 + (returns[i] if returns[i-1] > 0 else -returns[i])
    rng = random.Random(seed); random_value = 1.0
    for value in returns: random_value *= 1 + value * rng.choice((-1, 1))
    values = {"no_trade": 0.0, "buy_and_hold": ordered[-1][1]/ordered[0][1]-1,
              "momentum": momentum-1, "random_direction": random_value-1}
    return {**values, "god_eyes": strategy_return,
            "excess_vs_buy_and_hold": strategy_return-values["buy_and_hold"], "seed": seed}


def walk_forward(opportunities: Iterable[dict[str, object]], prices: dict[str, list[tuple[datetime, float]]],
                 config: PaperConfig | None = None) -> dict[str, object]:
    """Replay in event time; entries can only see prices at/before decision, exits only after horizon."""
    portfolio = PaperPortfolio(config)
    for opportunity in sorted(opportunities, key=lambda o: str(o.get("created_at", ""))):
        at = as_utc(opportunity["created_at"]); series = sorted(prices.get(str(opportunity.get("instrument")), []))
        past = [row for row in series if row[0] <= at]
        if not past: continue
        decision = portfolio.open(opportunity, past[-1][1], at)
        if decision["status"] != "opened": continue
        position_id = str(decision["position"]["position_id"]); hours = {"5m": 1/12, "1h": 1, "4h": 4, "24h": 24, "7d": 168}.get(str(opportunity.get("horizon")), 24)
        due = at + timedelta(hours=hours); future = [row for row in series if row[0] >= due]
        if future: portfolio.close(position_id, future[0][1], future[0][0])
    metrics = portfolio_metrics(portfolio)
    combined = sorted((row for series in prices.values() for row in series), key=lambda row: row[0])
    return {"strategy_version": portfolio.config.strategy_version, "metrics": metrics, "portfolio": portfolio.public(),
            "benchmarks": benchmark_comparison(combined, float(metrics["total_return"])), "look_ahead": False}
