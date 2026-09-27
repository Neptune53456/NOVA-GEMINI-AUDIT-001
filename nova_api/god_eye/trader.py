"""NOVA Trader V1: deterministic paper-only execution, accounting and research records."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from math import sqrt
from typing import Any, Iterable

from .costs import CostEngine
from .models import as_utc, utc_now


class TraderMode(StrEnum):
    OBSERVE = "OBSERVE"
    ASSISTED_PAPER = "ASSISTED_PAPER"
    AUTO_PAPER = "AUTO_PAPER"


STRATEGIES = ("MOMENTUM", "MEAN_REVERSION", "EVENT_DRIVEN", "PATTERN", "STAR_FINDER_ENSEMBLE", "NOVA_COMPOSITE")


@dataclass(frozen=True)
class TraderConfig:
    version: str = "nova-trader-v1"
    mode: TraderMode = TraderMode.OBSERVE
    base_currency: str = "EUR"
    starting_capital: float = 10_000.0
    max_position_fraction: float = .10
    max_gross_exposure: float = .75
    cash_reserve_fraction: float = .10
    liquidity_fraction: float = .10
    warning_drawdown: float = .08
    defensive_drawdown: float = .12
    halt_drawdown: float = .20
    switch_threshold: float = .003
    action_cooldown_seconds: int = 900
    minimum_action_fraction: float = .02


@dataclass
class PaperOrder:
    order_id: str; idempotency_key: str; instrument: str; side: str; requested_quantity: float
    requested_price: float; order_type: str; strategy: str; opportunity_id: str; created_at: str
    status: str = "pending"; filled_quantity: float = 0.0; rejection_reason: str | None = None
    model_version: str | None = None; config_version: str = "nova-trader-v1"


@dataclass
class PaperFill:
    fill_id: str; order_id: str; instrument: str; side: str; quantity: float
    requested_price: float; execution_price: float; notional: float; fees: float
    slippage: float; spread_cost: float; timestamp: str; venue: str | None
    strategy: str; opportunity_id: str; model_version: str | None; config_version: str


class PaperExecutionEngine:
    """No transport and no credentials: converts snapshots into deterministic paper fills."""
    def __init__(self, cost_engine: CostEngine | None = None) -> None:
        self.cost_engine = cost_engine or CostEngine(); self._orders: dict[str, tuple[PaperOrder, list[PaperFill]]] = {}

    def execute(self, request: dict[str, Any], *, microstructure: dict[str, Any] | None = None,
                available_cash: float, kill_switch: bool = False, at: datetime | None = None) -> dict[str, Any]:
        key = str(request.get("idempotency_key") or "")
        if not key: return {"status": "rejected", "reason": "idempotency_key_required", "paper_only": True}
        if key in self._orders:
            order, fills = self._orders[key]; return {"order": asdict(order), "fills": [asdict(v) for v in fills], "duplicate": True, "paper_only": True}
        now = as_utc(at or utc_now()); price = float(request.get("price", 0)); quantity = abs(float(request.get("quantity", 0)))
        side = str(request.get("side", "buy")).lower(); notional = price * quantity
        oid = sha256(key.encode()).hexdigest()
        order = PaperOrder(oid, key, str(request.get("instrument", "")), side, quantity, price,
            str(request.get("order_type", "market")), str(request.get("strategy", "NOVA_COMPOSITE")),
            str(request.get("opportunity_id", "")), now.isoformat(), model_version=request.get("model_version"))
        reason = None
        if kill_switch and side in {"buy", "add"}: reason = "kill_switch_active"
        elif price <= 0 or quantity <= 0: reason = "invalid_order"
        elif side in {"buy", "add"} and notional > available_cash: reason = "insufficient_cash"
        micro = microstructure or {}; depth_qty = float(micro.get("ask_depth" if side in {"buy", "add"} else "bid_depth", 0) or 0)
        fill_qty = min(quantity, depth_qty) if depth_qty > 0 else quantity
        if reason is None and fill_qty <= 0: reason = "insufficient_liquidity"
        if reason:
            order.status, order.rejection_reason = "rejected", reason; self._orders[key] = (order, [])
            return {"order": asdict(order), "fills": [], "paper_only": True}
        asset = str(request.get("asset_class", "crypto")); venue = request.get("venue")
        costs = self.cost_engine.estimate(asset_class=asset, venue=venue, size=price*fill_qty, price=price, microstructure=micro)
        direction = "up" if side in {"buy", "add"} else "down"
        execution = self.cost_engine.execution_prices(price, direction, costs)["realistic_entry_price"]
        if order.order_type == "limit" and ((side in {"buy", "add"} and execution > float(request.get("limit_price", price))) or
             (side not in {"buy", "add"} and execution < float(request.get("limit_price", price)))):
            order.status = "pending"; self._orders[key] = (order, []); return {"order": asdict(order), "fills": [], "paper_only": True}
        actual = abs(execution * fill_qty); fees = actual * float(costs["entry_fee"])
        fill = PaperFill(sha256(f"{oid}|0".encode()).hexdigest(), oid, order.instrument, side, fill_qty, price,
            execution, actual, fees, abs(execution-price)*fill_qty, price*fill_qty*float(costs["spread"])/2,
            now.isoformat(), str(venue) if venue else None, order.strategy, order.opportunity_id,
            order.model_version, str(costs["config_version"]))
        order.filled_quantity = fill_qty; order.status = "filled" if fill_qty == quantity else "partially_filled"
        self._orders[key] = (order, [fill])
        return {"order": asdict(order), "fills": [asdict(fill)], "cost_estimate": costs, "paper_only": True}

    def cancel(self, idempotency_key: str) -> dict[str, Any]:
        order, fills = self._orders[idempotency_key]
        if order.status in {"pending", "partially_filled"}: order.status = "cancelled"
        return {"order": asdict(order), "fills": [asdict(v) for v in fills], "paper_only": True}

    def restore(self, orders: Iterable[dict[str, Any]], fills: Iterable[dict[str, Any]]) -> None:
        """Restore the idempotency index before accepting any new paper action."""
        by_order: dict[str, list[PaperFill]] = {}
        for value in fills:
            fill = PaperFill(**{k: value.get(k) for k in PaperFill.__dataclass_fields__})
            by_order.setdefault(fill.order_id, []).append(fill)
        for value in orders:
            order = PaperOrder(**{k: value.get(k) for k in PaperOrder.__dataclass_fields__})
            self._orders[order.idempotency_key] = (order, by_order.get(order.order_id, []))


@dataclass
class LedgerPosition:
    position_id: str; instrument: str; strategy: str; opportunity_id: str; quantity: float
    average_price: float; opened_at: str; asset_class: str; venue: str | None; currency: str
    fees: float = 0.0; slippage_cost: float = 0.0; mfe: float = 0.0; mae: float = 0.0
    mfe_at: str | None = None; mae_at: str | None = None; last_action_at: str | None = None


class PaperLedger:
    MAX_RECENT_FILLS = 1000
    MAX_RECENT_CLOSED = 1000
    MAX_EQUITY_SNAPSHOTS = 1000

    def __init__(self, config: TraderConfig | None = None, *, portfolio_id: str = "NOVA_COMPOSITE") -> None:
        self.config = config or TraderConfig(); self.portfolio_id = portfolio_id; self.cash = self.config.starting_capital
        self.reserved_cash = 0.0; self.positions: dict[str, LedgerPosition] = {}; self.fills: list[dict[str, Any]] = []
        self._fill_ids: set[str] = set()
        self.closed: list[dict[str, Any]] = []; self.equity_snapshots: list[dict[str, Any]] = []; self.peak_equity = self.cash

    def apply_fill(self, fill: dict[str, Any], *, asset_class: str = "crypto", currency: str | None = None) -> LedgerPosition | None:
        fill_id = str(fill["fill_id"])
        if fill_id in self._fill_ids: return None
        qty = float(fill["quantity"]) * (1 if fill["side"] in {"buy", "add"} else -1); px = float(fill["execution_price"])
        fee = float(fill["fees"]); pid = f"{fill['strategy']}|{fill['instrument']}"; current = self.positions.get(pid)
        self.cash -= qty * px + fee; self.fills.append(dict(fill)); self._fill_ids.add(fill_id)
        if len(self.fills) > self.MAX_RECENT_FILLS:
            removed = self.fills[:-self.MAX_RECENT_FILLS]
            self.fills = self.fills[-self.MAX_RECENT_FILLS:]
            self._fill_ids.difference_update(str(v["fill_id"]) for v in removed)
        if current is None and qty != 0:
            current = LedgerPosition(pid, fill["instrument"], fill["strategy"], fill["opportunity_id"], qty, px,
                fill["timestamp"], asset_class, fill.get("venue"), currency or self.config.base_currency,
                fee, float(fill["slippage"]), last_action_at=fill["timestamp"]); self.positions[pid] = current
        elif current:
            old_qty = current.quantity; new_qty = old_qty + qty
            if old_qty * qty > 0: current.average_price = (abs(old_qty)*current.average_price + abs(qty)*px)/abs(new_qty)
            current.quantity = new_qty; current.fees += fee; current.slippage_cost += float(fill["slippage"]); current.last_action_at = fill["timestamp"]
            if abs(new_qty) < 1e-12:
                pnl = old_qty * (px-current.average_price) - current.fees
                self.closed.append({"position_id":pid,"opportunity_id":current.opportunity_id,"strategy":current.strategy,
                    "opened_at":current.opened_at,"closed_at":fill["timestamp"],"realized_pnl":pnl,"fees":current.fees,
                    "slippage":current.slippage_cost,"mfe":current.mfe,"mae":current.mae,
                    "entry_price":current.average_price,"exit_price":px,"closed_quantity":abs(old_qty),"direction":1 if old_qty>0 else -1,
                    "exit_capture_ratio":pnl/current.mfe if current.mfe > 0 else None})
                del self.positions[pid]; return None
        if len(self.closed) > self.MAX_RECENT_CLOSED: self.closed = self.closed[-self.MAX_RECENT_CLOSED:]
        return current

    def mark(self, prices: dict[str, float], at: datetime | str, fx: dict[str, float] | None = None) -> dict[str, Any]:
        unrealized = 0.0; gross = 0.0; by_asset: dict[str,float] = {}; by_venue: dict[str,float] = {}
        for p in self.positions.values():
            if p.instrument not in prices: continue
            value = p.quantity * prices[p.instrument]; pnl = p.quantity * (prices[p.instrument]-p.average_price)
            if p.currency != self.config.base_currency:
                rate = (fx or {}).get(f"{p.currency}/{self.config.base_currency}")
                if rate is None: continue
                value *= rate; pnl *= rate
            unrealized += pnl; gross += abs(value); by_asset[p.asset_class] = by_asset.get(p.asset_class,0)+abs(value)
            by_venue[str(p.venue or "unknown")] = by_venue.get(str(p.venue or "unknown"),0)+abs(value)
            if pnl > p.mfe: p.mfe, p.mfe_at = pnl, as_utc(at).isoformat()
            if pnl < p.mae: p.mae, p.mae_at = pnl, as_utc(at).isoformat()
        equity = self.cash + sum(p.quantity*prices.get(p.instrument,p.average_price) for p in self.positions.values())
        self.peak_equity=max(self.peak_equity,equity); drawdown=1-equity/self.peak_equity if self.peak_equity else 0
        snap={"timestamp":as_utc(at).isoformat(),"cash":self.cash,"reserved_cash":self.reserved_cash,"equity":equity,
            "unrealized_pnl":unrealized,"realized_pnl":sum(v["realized_pnl"] for v in self.closed),"gross_exposure":gross,
            "net_exposure":sum(p.quantity*prices.get(p.instrument,p.average_price) for p in self.positions.values()),
            "asset_class_exposure":by_asset,"venue_exposure":by_venue,"drawdown":drawdown,"peak_equity":self.peak_equity}
        self.equity_snapshots.append(snap)
        if len(self.equity_snapshots) > self.MAX_EQUITY_SNAPSHOTS: self.equity_snapshots = self.equity_snapshots[-self.MAX_EQUITY_SNAPSHOTS:]
        return snap

    def risk_budget(self, *, volatility: float = 0, concentration: float = 0, correlation: float = 0,
                    regime_risk: float = 0, uncertainty: float = 0) -> dict[str, Any]:
        dd = self.equity_snapshots[-1]["drawdown"] if self.equity_snapshots else 0
        penalty=min(.95,.30*volatility+.20*concentration+.15*correlation+.15*regime_risk+.20*uncertainty)
        state="normal"; multiplier=max(.05,1-penalty)
        if dd >= self.config.halt_drawdown: state,multiplier="halt-new-entries",0.0
        elif dd >= self.config.defensive_drawdown: state,multiplier="defensive",multiplier*.35
        elif dd >= self.config.warning_drawdown: state,multiplier="warning",multiplier*.65
        return {"state":state,"sizing_multiplier":multiplier,"new_entries_allowed":state!="halt-new-entries","drawdown":dd,"version":"risk-budget-v1"}

    def size(self, opportunity: dict[str, Any], *, policy: str = "nova_composite", liquidity_notional: float | None = None) -> float:
        equity = self.equity_snapshots[-1]["equity"] if self.equity_snapshots else self.config.starting_capital
        score=max(0,min(1,float(opportunity.get("star_score",50))/100)); uncertainty=max(0,min(1,float(opportunity.get("uncertainty",1))))
        fraction=self.config.max_position_fraction
        if policy=="score_weighted": fraction*=score
        elif policy=="volatility_adjusted": fraction*=min(1,.02/max(.001,float(opportunity.get("volatility",.02))))
        elif policy=="conservative_kelly": fraction*=min(.5,max(0,float(opportunity.get("expected_net_return",0))/.10))
        elif policy=="nova_composite": fraction*=score*(1-uncertainty)
        cap=min(equity*fraction, max(0,self.cash-equity*self.config.cash_reserve_fraction))
        if liquidity_notional is not None: cap=min(cap,liquidity_notional*self.config.liquidity_fraction)
        return max(0,cap*self.risk_budget(uncertainty=uncertainty)["sizing_multiplier"])

    def public(self) -> dict[str, Any]:
        last=self.equity_snapshots[-1] if self.equity_snapshots else {"equity":self.cash,"drawdown":0,"gross_exposure":0,"net_exposure":0}
        return {"portfolio_id":self.portfolio_id,"mode":self.config.mode,"base_currency":self.config.base_currency,
            "starting_capital":self.config.starting_capital,**last,"cash":self.cash,"reserved_cash":self.reserved_cash,
            "positions":[asdict(v) for v in self.positions.values()],
            "fees_paid":sum(float(v["fees"]) for v in self.fills),"slippage_cost":sum(float(v["slippage"]) for v in self.fills),"paper_only":True}

    def state(self) -> dict[str, Any]:
        return {"portfolio_id":self.portfolio_id,"cash":self.cash,"reserved_cash":self.reserved_cash,
            "positions":[asdict(v) for v in self.positions.values()],"fills":self.fills,"closed":self.closed,
            "equity_snapshots":self.equity_snapshots,"peak_equity":self.peak_equity,
            "config":{**asdict(self.config),"mode":self.config.mode.value},"paper_only":True}

    @classmethod
    def restore(cls, value: dict[str, Any] | None, fallback: TraderConfig | None = None) -> "PaperLedger":
        if not value: return cls(fallback)
        raw=dict(value.get("config",{})); raw["mode"]=TraderMode(raw.get("mode",TraderMode.OBSERVE))
        config=TraderConfig(**{k:v for k,v in raw.items() if k in TraderConfig.__dataclass_fields__})
        ledger=cls(config,portfolio_id=str(value.get("portfolio_id","NOVA_COMPOSITE")))
        ledger.cash=float(value.get("cash",config.starting_capital)); ledger.reserved_cash=float(value.get("reserved_cash",0))
        ledger.positions={str(v["position_id"]):LedgerPosition(**v) for v in value.get("positions",[])}
        ledger.fills=list(value.get("fills",[])); ledger.closed=list(value.get("closed",[]))
        ledger.fills=ledger.fills[-ledger.MAX_RECENT_FILLS:]; ledger._fill_ids={str(v["fill_id"]) for v in ledger.fills}
        ledger.closed=ledger.closed[-ledger.MAX_RECENT_CLOSED:]
        ledger.equity_snapshots=list(value.get("equity_snapshots",[])); ledger.peak_equity=float(value.get("peak_equity",ledger.cash))
        ledger.equity_snapshots=ledger.equity_snapshots[-ledger.MAX_EQUITY_SNAPSHOTS:]
        return ledger


class ReplayEngine:
    """Deterministic, offline reconstruction from persisted point-in-time records."""
    def replay(self, records: Iterable[dict[str, Any]], *, starting_capital: float = 10_000) -> dict[str, Any]:
        rows=list(records)
        # V2 accepts an event envelope (kind/payload) as well as legacy bare fills.
        normalized=[]; enveloped=any("kind" in row for row in rows)
        for row in rows:
            payload=dict(row.get("payload",row)); kind=str(row.get("kind") or ("fill" if payload.get("fill_id") else ""))
            normalized.append((kind,payload))
        kinds={kind for kind,_ in normalized if kind}
        required={"market_snapshot","forecast","opportunity","decision","order","fill","portfolio_transition","outcome"}
        if enveloped and not required.issubset(kinds):
            return {"status":"insufficient_replay_data","missing_stages":sorted(required-kinds),
                    "network_calls":0,"broker_calls":0,"production_writes":0,"paper_only":True,"deterministic":True}
        ledger=PaperLedger(TraderConfig(starting_capital=starting_capital))
        ordered=sorted((value for kind,value in normalized if kind=="fill" or (not enveloped and value.get("fill_id"))),
                       key=lambda v:(str(v.get("timestamp") or v.get("created_at") or ""),str(v.get("fill_id") or "")))
        seen=set()
        for value in ordered:
            if not value.get("fill_id") or value["fill_id"] in seen: continue
            seen.add(value["fill_id"]); ledger.apply_fill(value,asset_class=str(value.get("asset_class","crypto")))
        return {"status":"ok","events":len(rows),"replayed_stages":sorted(kinds) if kinds else ["fill"],
                "unique_fills":len(seen),"state":ledger.state(),"performance":performance(ledger),
                "network_calls":0,"broker_calls":0,"production_writes":0,"paper_only":True,"deterministic":True}


def approximate_attribution(opportunity: dict[str, Any], fill: dict[str, Any] | None = None) -> dict[str, Any]:
    components={"momentum":0.0,"mean_reversion":0.0,"pattern":0.0,"events":0.0,"regime":0.0,
        "historical_similarity":0.0,"microstructure":0.0,"costs":0.0,"sizing":0.0,"entry_timing":0.0,"exit_timing":0.0}
    explanation=opportunity.get("score_explanation",{}).get("components",{})
    for key in components:
        if key in explanation: components[key]=float(explanation[key])
    components["pattern"]+=sum(float(v.get("strength",0)) for v in opportunity.get("supporting_patterns",[]))
    components["events"]+=min(1.0,len(opportunity.get("supporting_events",[]))*.1)
    components["regime"]=float(opportunity.get("regime_stability",0)); components["historical_similarity"]=1-float(opportunity.get("uncertainty",1))
    components["costs"]=-float(opportunity.get("expected_costs",0) or 0)
    if fill: components["entry_timing"]=-float(fill.get("slippage",0))
    return {"method":"approximate_analytical_v1","causal":False,"components":components,"paper_only":True}


def missed_outcome(decision: dict[str, Any], entry_price: float, exit_price: float, *, costs: float = 0.0) -> dict[str, Any]:
    gross=(exit_price/entry_price-1) if entry_price>0 else 0.0; net=gross-costs
    classification=("missed_winner" if net>costs else
                    "correctly_avoided" if net<0 and decision.get("action") in {"IGNORE","REJECTED"} else
                    "avoided_loss" if net<0 else "ambiguous")
    return {"opportunity_id":decision.get("opportunity_id"),"decision":decision.get("action","WAIT"),
        "forward_return":gross,"net_result":net,"costs":costs,"classification":classification,
        "point_in_time":True,"hypothetical":True,"paper_only":True}


def strategy_competition(snapshots: Iterable[dict[str, Any]], *, starting_capital: float = 10_000) -> dict[str, Any]:
    rows=list(snapshots); results={}
    for strategy in STRATEGIES:
        selected=[v for v in rows if v.get("strategy")==strategy or v.get("portfolio_id")==strategy]
        sim=capital_simulation(selected,starting_capital)
        regimes={str(v.get("regime","unknown")) for v in selected}; assets={str(v.get("asset_class","unknown")) for v in selected}
        horizons={str(v.get("horizon","unknown")) for v in selected}
        results[strategy]={**sim,"isolated_ledger":strategy,"shared_snapshot_count":len(rows),
            "turnover":sum(abs(float(v.get("notional",0))) for v in selected),"stability":None,
            "regime_breakdown":sorted(regimes),"asset_class_breakdown":sorted(assets),
            "horizon_breakdown":sorted(horizons),"sample_size":len(selected)}
    return {"strategies":results,"same_cost_engine":True,"same_execution_assumptions":True,"paper_only":True}


def position_action(position: LedgerPosition, opportunity: dict[str, Any] | None, *, risk_ok: bool = True,
                    now: datetime | None = None, config: TraderConfig | None = None) -> dict[str, Any]:
    c=config or TraderConfig(); at=as_utc(now or utc_now())
    if position.last_action_at and (at-as_utc(position.last_action_at)).total_seconds()<c.action_cooldown_seconds:
        return {"action":"HOLD","reason":"cooldown","paper_only":True}
    if not risk_ok or not opportunity or opportunity.get("thesis_invalidated"): action,reason="EXIT","risk_or_thesis_invalid"
    elif float(opportunity.get("expected_net_return",0)) <= float(opportunity.get("action_cost",0)): action,reason="EXIT","net_edge_gone"
    elif float(opportunity.get("uncertainty",0))>.6: action,reason="REDUCE","uncertainty"
    elif opportunity.get("new_independent_evidence") and float(opportunity.get("expected_net_return",0))>.01: action,reason="ADD","strengthened"
    else: action,reason="HOLD","insignificant_change"
    return {"action":action,"reason":reason,"paper_only":True,"version":"position-manager-v1"}


def switch_advantage(current: dict[str, Any], replacement: dict[str, Any], *, exit_cost: float, entry_cost: float,
                     additional_risk: float = 0, threshold: float = .003) -> dict[str, Any]:
    advantage=float(replacement.get("expected_net_return",0))-float(current.get("expected_net_return",0))-exit_cost-entry_cost-additional_risk
    return {"switch":advantage>threshold,"net_advantage":advantage,"threshold":threshold,"paper_only":True}


def performance(ledger: PaperLedger) -> dict[str, Any]:
    snaps=ledger.equity_snapshots; trades=ledger.closed; initial=ledger.config.starting_capital
    end=float(snaps[-1]["equity"]) if snaps else ledger.cash; returns=[float(snaps[i]["equity"])/float(snaps[i-1]["equity"])-1 for i in range(1,len(snaps)) if snaps[i-1]["equity"]]
    wins=[v["realized_pnl"] for v in trades if v["realized_pnl"]>0]; losses=[v["realized_pnl"] for v in trades if v["realized_pnl"]<0]
    mean=sum(returns)/len(returns) if returns else 0; sd=(sum((v-mean)**2 for v in returns)/(len(returns)-1))**.5 if len(returns)>1 else 0
    downside=[v for v in returns if v<0]; dsd=(sum(v*v for v in downside)/len(downside))**.5 if downside else 0
    return {"ending_equity":end,"net_pnl":end-initial,"total_return":end/initial-1,"trade_count":len(trades),
        "hit_rate":len(wins)/len(trades) if trades else None,"profit_factor":sum(wins)/abs(sum(losses)) if losses else None,
        "average_win":sum(wins)/len(wins) if wins else None,"average_loss":sum(losses)/len(losses) if losses else None,
        "expectancy":sum(v["realized_pnl"] for v in trades)/len(trades) if trades else None,
        "max_drawdown":max((v["drawdown"] for v in snaps),default=0),"volatility":sd*sqrt(252) if len(returns)>=20 else None,
        "sharpe":mean/sd*sqrt(252) if len(returns)>=30 and sd else None,"sortino":mean/dsd*sqrt(252) if len(returns)>=30 and dsd else None,
        "fees":sum(float(v["fees"]) for v in ledger.fills),"slippage":sum(float(v["slippage"]) for v in ledger.fills),
        "metrics_reliable":len(trades)>=20,"paper_only":True}


def capital_simulation(events: Iterable[dict[str, Any]], starting_capital: float) -> dict[str, Any]:
    ordered=sorted(events,key=lambda v:str(v.get("timestamp","")))
    if not ordered: return {"status":"insufficient_point_in_time_data","starting_capital":starting_capital,"paper_only":True}
    pnl=sum(float(v.get("realized_pnl",0)) for v in ordered); fees=sum(float(v.get("fees",0)) for v in ordered); slip=sum(float(v.get("slippage",0)) for v in ordered)
    end=starting_capital+pnl
    return {"status":"ok","starting_capital":starting_capital,"ending_equity":end,"net_return":end/starting_capital-1,
        "gross_pnl":pnl+fees+slip,"net_pnl":pnl,"fees":fees,"slippage":slip,"trades":len(ordered),"paper_only":True,"look_ahead":False}
