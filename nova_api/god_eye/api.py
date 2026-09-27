"""FastAPI router for the God Eyes read-only data foundation."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .service import GodEyeService


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: bool = True
    news: bool = True
    history: bool = True
    symbols: list[str] | None = Field(default=None, max_length=100)

class LiveForwardStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_version: str = Field(min_length=1, max_length=100)
    market_universe: list[str] = Field(min_length=1, max_length=100)
    params: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

class TraderModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str


def build_router(service: GodEyeService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/god-eye", tags=["god-eye"])

    @router.get("/market")
    def market(symbol: list[str] | None = Query(default=None)) -> dict[str, object]:
        return service.get_market_snapshot(symbol)

    @router.get("/news")
    def news(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
        return service.get_recent_news(limit)

    @router.get("/social")
    def social(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
        return service.get_social(limit)

    @router.get("/social/signals")
    def social_signals(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
        return service.get_social_signals(limit)

    @router.get("/portfolio")
    def portfolio() -> dict[str, object]:
        return service.get_portfolio()

    @router.get("/portfolio/trades")
    def portfolio_trades(limit: int = Query(default=500, ge=1, le=1000)) -> dict[str, object]:
        return service.get_portfolio_trades(limit)

    @router.get("/walk-forward")
    def walk_forward() -> dict[str, object]:
        return service.get_walk_forward()

    @router.get("/benchmarks")
    def benchmarks() -> dict[str, object]:
        return service.get_benchmarks()

    @router.get("/alerts")
    def alerts(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]: return service.get_alerts(limit)

    @router.post("/alerts/{alert_id}/acknowledge")
    def acknowledge_alert(alert_id: str) -> dict[str, object]: return service.acknowledge_alert(alert_id)

    @router.get("/governance")
    def governance() -> dict[str, object]: return service.get_governance()

    @router.get("/influence-graph")
    def influence_graph() -> dict[str, object]: return service.get_influence_graph()

    @router.get("/experiments")
    def experiments() -> dict[str, object]: return service.get_research("experiment")
    @router.get("/candidates")
    def candidates() -> dict[str, object]: return service.get_research("candidate")
    @router.get("/incumbent")
    def incumbent() -> dict[str, object]: return service.get_research("ensemble")
    @router.get("/live-forward")
    def live_forward() -> dict[str, object]: return service.get_live_forward()
    @router.post("/live-forward")
    def start_live_forward(request: LiveForwardStartRequest) -> dict[str, object]:
        return service.live_forward.start(request.model_version, request.market_universe, params=request.params)
    @router.post("/live-forward/{validation_id}/update")
    def update_live_forward(validation_id: str) -> dict[str, object]:
        try: return service.live_forward.update(validation_id)
        except KeyError: raise HTTPException(status_code=404, detail="live_forward_not_found")
    @router.get("/data-integrity")
    def data_integrity() -> dict[str, object]: return service.get_data_integrity()
    @router.get("/alternative-data/health")
    def alternative_health() -> dict[str, object]: return service.get_alternative_health()

    @router.get("/health")
    def health() -> dict[str, object]:
        return service.get_data_health()

    @router.get("/events")
    def events(limit: int = Query(default=100, ge=1, le=500),
               event_type: str | None = Query(default=None), instrument: str | None = Query(default=None),
               start: datetime | None = Query(default=None), end: datetime | None = Query(default=None)) -> dict[str, object]:
        return service.get_events(limit,event_type,instrument,start.isoformat() if start else None,end.isoformat() if end else None)

    @router.get("/events/{event_id}")
    def event(event_id: str) -> dict[str, object]:
        value = service.get_event(event_id)
        if value is None:
            raise HTTPException(status_code=404, detail="event_not_found")
        return value

    @router.get("/events/{event_id}/analysis")
    def event_analysis(event_id: str) -> dict[str, object]:
        value = service.get_event_analysis(event_id)
        if value is None:
            raise HTTPException(status_code=404, detail="event_not_found")
        return value

    @router.get("/events/{event_id}/similar")
    def similar_events(event_id: str, horizon: str = Query(default="24h", pattern="^(5m|1h|4h|24h|7d)$"),
                       top_k: int = Query(default=5, ge=1, le=20)) -> dict[str, object]:
        value = service.get_similar_events(event_id, horizon, top_k)
        if value is None:
            raise HTTPException(status_code=404, detail="event_not_found")
        return value

    @router.get("/history/{instrument}")
    def history(instrument: str, interval: str | None = Query(default=None, pattern="^(1m|5m|1h|1d)$"),
                limit: int = Query(default=500, ge=1, le=2000), start: datetime | None = Query(default=None),
                end: datetime | None = Query(default=None)) -> dict[str, object]:
        if start and end and start > end: raise HTTPException(status_code=422,detail="invalid_time_range")
        return service.get_history(instrument,interval,limit,start.isoformat() if start else None,end.isoformat() if end else None)

    @router.get("/scheduler")
    def scheduler() -> dict[str, object]:
        return service.scheduler.status()

    @router.get("/forecasts")
    def forecasts(limit: int = Query(default=100, ge=1, le=500),instrument: str | None = Query(default=None),
                  start: datetime | None = Query(default=None),end: datetime | None = Query(default=None)) -> dict[str, object]:
        return service.get_forecasts(limit,instrument,start.isoformat() if start else None,end.isoformat() if end else None)

    @router.get("/forecasts/{forecast_id}")
    def forecast(forecast_id: str) -> dict[str, object]:
        value = service.get_forecast(forecast_id)
        if value is None:
            raise HTTPException(status_code=404, detail="forecast_not_found")
        return value

    @router.get("/performance")
    def performance() -> dict[str, object]:
        return service.get_performance()

    @router.get("/calibration")
    def calibration() -> dict[str, object]:
        return service.get_calibration()

    @router.get("/regimes")
    def regimes() -> dict[str, object]:
        return service.get_regimes()

    @router.get("/scanner")
    def scanner() -> dict[str, object]: return service.get_scanner()
    @router.post("/scanner/run")
    def run_scanner() -> dict[str, object]: return service.run_scanner()
    @router.get("/patterns")
    def patterns(instrument: str | None = None,limit: int = Query(default=100,ge=1,le=500),
                 start: datetime | None = Query(default=None),end: datetime | None = Query(default=None)) -> dict[str, object]:
        return service.get_patterns(instrument,limit,start.isoformat() if start else None,end.isoformat() if end else None)
    @router.get("/intelligence/{instrument}")
    def intelligence(instrument: str) -> dict[str, object]:
        try: return service.get_market_intelligence(instrument)
        except KeyError: raise HTTPException(status_code=404, detail="instrument_not_found")
    @router.get("/microstructure/{instrument}")
    def microstructure(instrument: str) -> dict[str, object]: return service.get_microstructure(instrument)
    @router.post("/microstructure/{instrument}/refresh")
    def refresh_microstructure(instrument: str) -> dict[str, object]: return service.refresh_microstructure(instrument)
    @router.get("/pattern-statistics")
    def pattern_statistics() -> dict[str, object]: return service.get_pattern_statistics()
    @router.get("/market-memory/{instrument}")
    def market_memory(instrument: str, minimum_samples: int = Query(default=3, ge=1, le=100)) -> dict[str, object]:
        try: return service.get_market_memory(instrument, minimum_samples)
        except KeyError: raise HTTPException(status_code=404, detail="instrument_not_found")

    @router.get("/opportunities")
    def opportunities(include_rejected: bool = False, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
        return service.get_opportunities(include_rejected, limit)

    @router.get("/opportunities/{opportunity_id}")
    def opportunity(opportunity_id: str) -> dict[str, object]:
        value = service.get_opportunity(opportunity_id)
        if value is None:
            raise HTTPException(status_code=404, detail="opportunity_not_found")
        return value

    @router.get("/star-finder/status")
    def star_finder_status() -> dict[str, object]: return service.get_star_finder_status()
    @router.get("/trader/status")
    def trader_status() -> dict[str, object]: return service.get_trader_status()
    @router.post("/trader/mode")
    def trader_mode(request: TraderModeRequest) -> dict[str, object]:
        try:return service.set_trader_mode(request.mode)
        except ValueError:raise HTTPException(status_code=422,detail="invalid_trader_mode")
    @router.get("/trader/portfolio")
    def trader_portfolio() -> dict[str, object]: return service.get_trader_portfolio()
    @router.get("/trader/positions")
    def trader_positions() -> dict[str, object]: return service.get_trader_positions()
    @router.get("/trader/orders")
    def trader_orders(limit: int = Query(default=500, ge=1, le=1000)) -> dict[str, object]: return service.get_trader_records("order",limit)
    @router.get("/trader/fills")
    def trader_fills(limit: int = Query(default=500, ge=1, le=1000)) -> dict[str, object]: return service.get_trader_records("fill",limit)
    @router.get("/trader/journal")
    def trader_journal(limit: int = Query(default=500, ge=1, le=1000)) -> dict[str, object]: return service.get_trader_records("journal",limit)
    @router.get("/trader/performance")
    def trader_performance() -> dict[str, object]: return service.get_trader_performance()
    @router.get("/trader/capital-simulation")
    def trader_capital_simulation(capital: float = Query(default=1000, gt=0, le=100_000_000)) -> dict[str, object]:
        return service.simulate_capital(capital)
    @router.get("/trader/replay")
    def trader_replay() -> dict[str, object]: return service.replay_trader()
    @router.get("/trader/strategy-competition")
    def trader_strategy_competition() -> dict[str, object]: return service.get_strategy_competition()
    @router.get("/trader/strategy-ledgers")
    def trader_strategy_ledgers() -> dict[str, object]: return service.get_strategy_ledgers()
    @router.get("/trader/decisions")
    def trader_decisions(limit: int = Query(default=500, ge=1, le=1000)) -> dict[str, object]:
        return {"decisions":service.store.decisions(limit),"paper_only":True}
    @router.get("/trader/outcomes")
    def trader_outcomes(limit: int = Query(default=500, ge=1, le=1000)) -> dict[str, object]:
        return service.get_trader_records("outcome",limit)
    @router.get("/trader/lifecycle/{opportunity_id}")
    def trader_lifecycle(opportunity_id: str) -> dict[str, object]: return service.get_opportunity_history(opportunity_id)
    @router.get("/trader/markers/{instrument}")
    def trader_markers(instrument: str,limit: int = Query(default=500,ge=1,le=1000)) -> dict[str, object]:
        return service.get_trade_markers(instrument,limit)
    @router.get("/trader/trades/{trade_id}")
    def trader_trade(trade_id: str) -> dict[str, object]:
        value=service.get_trade_detail(trade_id)
        if value is None:raise HTTPException(status_code=404,detail="paper_trade_not_found")
        return value
    @router.get("/research/attribution")
    def research_attribution() -> dict[str, object]: return service.get_attribution()
    @router.get("/research/status")
    def research_status() -> dict[str, object]: return service.get_research_status()
    @router.get("/research/decision-quality")
    def decision_quality() -> dict[str, object]: return service.get_decision_learning()
    @router.get("/endurance/health")
    def endurance_health() -> dict[str, object]: return service.get_endurance_health()
    @router.post("/star-finder/scan")
    def star_finder_scan() -> dict[str, object]: return service.run_star_finder()
    @router.get("/star-finder/opportunities")
    def star_finder_opportunities(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
        return service.get_ranked_opportunities(limit)
    @router.get("/star-finder/opportunities/{opportunity_id}")
    def star_finder_opportunity(opportunity_id: str) -> dict[str, object]:
        value=service.get_star_opportunity(opportunity_id)
        if value is None: raise HTTPException(status_code=404,detail="opportunity_not_found")
        return value
    @router.get("/star-finder/opportunities/{opportunity_id}/history")
    def star_finder_history(opportunity_id: str) -> dict[str, object]: return service.get_opportunity_history(opportunity_id)
    @router.get("/star-finder/costs")
    def star_finder_costs() -> dict[str, object]: return service.get_costs()
    @router.get("/star-finder/venues/{instrument}")
    def star_finder_venues(instrument: str, size: float = Query(default=1000, gt=0, le=10_000_000)) -> dict[str, object]:
        try:return service.compare_venues(instrument,size)
        except KeyError:raise HTTPException(status_code=404,detail="instrument_not_found")

    @router.post("/refresh")
    def refresh(request: RefreshRequest) -> dict[str, object]:
        return {"market": service.refresh_market(request.symbols) if request.market else None,
                "history": service.refresh_history(request.symbols) if request.history else None,
                "news": service.refresh_news() if request.news else None}

    @router.post("/alternative-data/refresh")
    def refresh_alternative() -> dict[str, object]: return service.refresh_alternative()

    return router
