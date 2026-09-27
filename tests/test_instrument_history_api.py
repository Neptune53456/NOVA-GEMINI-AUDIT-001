from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from nova_api.app import create_app
from nova_api.god_eye.models import (DataQuality, ForecastRecord, GodEyeEvent, InstrumentType,
                                     MarketCandle, MarketInstrument, SourceMetadata)
from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.star_finder import lifecycle_transition
from nova_api.god_eye.storage import GodEyeStore
from nova_api.journal import EventJournal


INSTRUMENT = MarketInstrument("BTC-USD", "Bitcoin", InstrumentType.CRYPTO)


def client_and_service(tmp_path):
    store = GodEyeStore(tmp_path / "god.sqlite3")
    service = GodEyeService(store=store, instruments=[INSTRUMENT], crypto_providers=[], news_providers=[], source_configs=[])
    client = TestClient(create_app(journal=EventJournal(tmp_path / "events.sqlite3"), god_eye_service=service))
    return client, service


def candle(at: datetime, close: float, provider: str = "test") -> MarketCandle:
    source = SourceMetadata(provider, "https://example.test/candles", at)
    return MarketCandle(INSTRUMENT,"1h",at,close-1,close+1,close-2,close,100.0,source,DataQuality(at,at,7200))


def test_candle_read_is_bounded_chronological_and_deduplicated(tmp_path):
    client, service = client_and_service(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    service.store.save_candles([candle(start,100),candle(start+timedelta(hours=1),101),candle(start+timedelta(hours=1),101,"z")])

    response = client.get("/api/v1/god-eye/history/BTC-USD?interval=1h&limit=2")
    value = response.json()

    assert response.status_code == 200
    assert [row["close"] for row in value["candles"]] == [100,101]
    assert value["available_timeframes"] == ["1h"]
    assert value["freshness"]["latest_at"] == (start+timedelta(hours=1)).isoformat()
    assert client.get("/api/v1/god-eye/history/BTC-USD?start=2026-02-01T00:00:00Z").json()["candles"] == []
    assert client.get("/api/v1/god-eye/history/BTC-USD?start=2026-02-02T00:00:00Z&end=2026-02-01T00:00:00Z").status_code == 422


def test_trade_markers_are_instrument_filtered_and_paper_only(tmp_path):
    client, service = client_and_service(tmp_path)
    at = datetime(2026,1,1,tzinfo=timezone.utc)
    fill = {"fill_id":"f1","order_id":"o1","instrument":"BTC-USD","side":"buy","quantity":2.0,
        "requested_price":100.0,"execution_price":101.0,"notional":202.0,"fees":1.0,"slippage":2.0,
        "spread_cost":0.5,"timestamp":at.isoformat(),"venue":"paper","strategy":"NOVA_COMPOSITE",
        "opportunity_id":"opp1","model_version":"v1","config_version":"v1"}
    service.store.save_trader_record("fill","f1",at.isoformat(),"NOVA_COMPOSITE",fill)
    service.store.save_lifecycle(lifecycle_transition("opp1","QUALIFIED","ENTER",reasons=["auto_paper_fill"],at=at,trade_ref="o1"))

    value = client.get("/api/v1/god-eye/trader/markers/BTC-USD").json()

    assert value["paper_only"] is True
    assert value["markers"] == [{"action":"ENTER","timestamp":at.isoformat(),"execution_price":101.0,
        "quantity":2.0,"notional":202.0,"fees":1.0,"slippage":2.0,"strategy":"NOVA_COMPOSITE",
        "opportunity_id":"opp1","position_id":"NOVA_COMPOSITE|BTC-USD","trade_id":"o1",
        "reason":["auto_paper_fill"],"lifecycle_status":"ENTER"}]
    assert client.get("/api/v1/god-eye/trader/markers/NVDA").json()["markers"] == []


def test_overlay_filters_and_trade_detail_are_read_only(tmp_path):
    client, service = client_and_service(tmp_path)
    at = datetime(2026,1,1,tzinfo=timezone.utc)
    event = GodEyeEvent("e1","Bitcoin news","Verified summary","NEWS",("Bitcoin",),("BTC-USD",),("feed",),at,at,.8,.9,{"label":"high"},("source:1",))
    forecast = ForecastRecord("fc1","BTC-USD","1h","baseline","v1","hash",.2,"up",{},at,at+timedelta(hours=1),probability_up=None)
    service.store.save_events([event]);service.store.save_forecast(forecast)
    service.store.save_star_opportunity({"opportunity_id":"opp1","detected_at":at.isoformat(),"status":"QUALIFIED","instrument":"BTC-USD"})
    service.store.save_lifecycle(lifecycle_transition("opp1",None,"DETECTED",reasons=["scanner_candidate"],at=at))
    trade={"position_id":"NOVA_COMPOSITE|BTC-USD","opportunity_id":"opp1","strategy":"NOVA_COMPOSITE",
        "instrument":"BTC-USD","closed_at":at.isoformat(),"realized_pnl":4.0,"fees":1.0,"slippage":.5,"mfe":8.0,"mae":-2.0}
    service.store.save_trader_record("trade",trade["position_id"],at.isoformat(),"NOVA_COMPOSITE",trade)
    before=len(service.store.trader_records("trade"))

    assert client.get("/api/v1/god-eye/events?instrument=BTC-USD").json()["events"][0]["event_id"] == "e1"
    assert client.get("/api/v1/god-eye/events?instrument=NVDA").json()["events"] == []
    filtered=client.get("/api/v1/god-eye/forecasts?instrument=BTC-USD").json()["forecasts"]
    assert filtered[0]["probability_up"] is None and filtered[0]["raw_score"] == .2
    detail=client.get("/api/v1/god-eye/trader/trades/NOVA_COMPOSITE%7CBTC-USD").json()
    assert detail["paper_only"] is True and detail["timeline"][0]["to_status"] == "DETECTED"
    assert len(service.store.trader_records("trade")) == before
