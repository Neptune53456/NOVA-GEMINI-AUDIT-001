import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from nova_api.app import create_app
from nova_api.god_eye.enrichment import EnrichmentGate, ModelRouterEventEnricher
from nova_api.god_eye.forecasting import (BaselineForecastEngine, build_feature_set,
                                          evaluate_forecast, reaction_features)
from nova_api.god_eye.intelligence import EventIntelligence
from nova_api.god_eye.models import (DataQuality, InstrumentType, MarketCandle, MarketInstrument,
                                     NewsItem, NewsSourceConfig, SourceKind, SourceMetadata, public)
from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.storage import GodEyeStore
from nova_api.journal import EventJournal

INSTRUMENT = MarketInstrument("BTC-USD", "Bitcoin", InstrumentType.CRYPTO, entities=("Bitcoin",))
SOURCE = NewsSourceConfig("official", "Official", "https://official.example/rss", "rss", 0.95,
                          ("official.example",), SourceKind.PRIMARY, True)
NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def seeded(tmp_path):
    store = GodEyeStore(tmp_path / "god.sqlite3")
    items = [NewsItem("official-proof", "Bitcoin quarterly results", "https://official.example/1", NOW,
                      "Revenue increased after product demand", SourceMetadata("official", SOURCE.url, NOW)),
             NewsItem("second-proof", "Bitcoin results confirmed", "https://secondary.example/2", NOW,
                      "Independent confirmation", SourceMetadata("secondary", "https://secondary.example/rss", NOW))]
    store.save_news(items)
    engine = EventIntelligence([INSTRUMENT], {"official": SOURCE})
    events = engine.build(items)
    store.save_events(events)
    return store, events[0].event_id


def response(direction="positive"):
    return {"message": {"content": json.dumps({
        "financial_summary": "Results improved.", "entities": ["Bitcoin"], "instruments": ["BTC-USD"],
        "impact_type": "fundamental", "potential_direction": direction, "horizons": ["hours", "1d"],
        "importance": 0.8, "novelty": 0.9, "interpretation_confidence": 0.75,
        "evidence_refs": ["official-proof", "second-proof"], "reasoning_summary": "Revenue improved.",
        "contradictions": []})}, "_meta": {"provider": "fake", "model": "fake-1"},
        "usage": {"input_tokens": 100, "output_tokens": 50}}


def candles(count=30, start=NOW - timedelta(minutes=10), step=timedelta(minutes=5)):
    return [{"opened_at": (start + step * i).isoformat(), "close": 100 + i, "open": 99 + i,
             "high": 101 + i, "low": 98 + i, "volume": 10 + i, "interval": "5m"} for i in range(count)]


def test_llm_gate_and_strict_output_validation():
    event = {"novelty_score": 1.0, "source_quality_score": 0.9, "instruments": ["BTC-USD"],
             "event_type": "earnings/results", "confidence": {"score": 0.5}}
    assert EnrichmentGate().eligible(event, remaining_budget=1) == (True, "eligible")
    assert EnrichmentGate().eligible(event, remaining_budget=0)[1] == "budget_exhausted"
    valid = ModelRouterEventEnricher(call=lambda *args, **kwargs: response())
    analysis, meta = valid.enrich({"event_id": "e", **event}, [
        {"item_id": "official-proof", "title": "A", "summary": "B", "source": {"provider": "official"}},
        {"item_id": "second-proof", "title": "C", "summary": "D", "source": {"provider": "secondary"}}])
    assert analysis["potential_direction"] == "positive" and meta["provider"] == "fake"
    with pytest.raises((ValueError, json.JSONDecodeError)):
        ModelRouterEventEnricher(call=lambda *a, **k: {"message": {"content": "not-json"}}).enrich(
            {"event_id": "e", **event}, [])


def test_enrichment_cache_multi_evidence_and_failure_is_closed(tmp_path):
    store, event_id = seeded(tmp_path)
    calls = []
    enricher = ModelRouterEventEnricher(call=lambda *a, **k: calls.append(a[0]) or response())
    service = GodEyeService(store=store, instruments=[INSTRUMENT], source_configs=[SOURCE],
                            news_providers=[], crypto_providers=[], event_enricher=enricher, llm_enabled=True)
    first = service.analyze_event(event_id)
    second = service.analyze_event(event_id)
    assert first["enrichment_status"] == "created" and second["enrichment_status"] == "cached"
    assert len(calls) == 1 and "second-proof" in calls[0][1]["content"]
    broken_store, broken_id = seeded(tmp_path / "broken")
    broken = GodEyeService(store=broken_store, instruments=[INSTRUMENT], source_configs=[SOURCE],
                           news_providers=[], crypto_providers=[], llm_enabled=True,
                           event_enricher=ModelRouterEventEnricher(call=lambda *a, **k: response("BUY")))
    assert broken.analyze_event(broken_id)["enrichment_status"] == "failed"
    assert broken_store.enrichment(broken_id) is None


def test_reactions_have_no_lookahead_and_feature_baseline_is_not_probability():
    values = candles()
    partial = reaction_features("e", "BTC-USD", NOW, values, as_of=NOW + timedelta(minutes=30))
    assert "5m" in partial.reactions and "1h" not in partial.reactions
    complete = reaction_features("e", "BTC-USD", NOW, values, as_of=NOW + timedelta(hours=2))
    assert "1h" in complete.reactions and complete.reactions["1h"]["max_favorable_excursion"] > 0
    event = {"event_type": "earnings/results", "source_quality_score": .9, "novelty_score": 1.0}
    features = build_feature_set("BTC-USD", values, event,
                                 {"potential_direction": "positive", "interpretation_confidence": .8},
                                 as_of=NOW + timedelta(hours=2))
    forecast = BaselineForecastEngine().forecast(features, "1h")
    assert forecast.direction in {"up", "down", "neutral"}
    assert -1 <= forecast.raw_score <= 1
    assert "probability" not in public(forecast)


def test_forecast_persistence_and_evaluation_are_idempotent(tmp_path):
    store = GodEyeStore(tmp_path / "forecast.sqlite3")
    values = candles(count=40)
    features = build_feature_set("BTC-USD", values[:20],
        {"event_type": "macro", "source_quality_score": .8, "novelty_score": .8}, as_of=NOW + timedelta(hours=1))
    forecast = BaselineForecastEngine().forecast(features, "1h")
    assert store.save_forecast(forecast) is True and store.save_forecast(forecast) is False
    evaluation = evaluate_forecast(public(forecast), values, as_of=forecast.due_at)
    assert evaluation is not None
    assert store.save_evaluation(forecast.forecast_id, evaluation) is True
    assert store.save_evaluation(forecast.forecast_id, evaluation) is False
    metrics = store.performance()[0]
    assert metrics["count"] == 1 and metrics["unresolved_count"] == 0


def test_phase3_apis(tmp_path):
    store, event_id = seeded(tmp_path)
    event = store.event(event_id)
    values = candles()
    source = SourceMetadata("test", "https://test.example", NOW)
    normalized = [MarketCandle(INSTRUMENT, "5m", datetime.fromisoformat(v["opened_at"]), v["open"], v["high"],
                  v["low"], v["close"], v["volume"], source, DataQuality(datetime.fromisoformat(v["opened_at"]), NOW, 600))
                  for v in values]
    store.save_candles(normalized)
    features = build_feature_set("BTC-USD", values, event, as_of=NOW + timedelta(hours=1))
    forecast = BaselineForecastEngine().forecast(features, "1h")
    store.save_forecast(forecast)
    service = GodEyeService(store=store, instruments=[INSTRUMENT], news_providers=[], crypto_providers=[], llm_enabled=False)
    client = TestClient(create_app(journal=EventJournal(tmp_path / "journal.sqlite3"), god_eye_service=service))
    assert client.get("/api/v1/god-eye/forecasts").status_code == 200
    assert client.get(f"/api/v1/god-eye/forecasts/{forecast.forecast_id}").status_code == 200
    assert client.get("/api/v1/god-eye/forecasts/missing").status_code == 404
    assert client.get("/api/v1/god-eye/performance").status_code == 200
    analysis = client.get(f"/api/v1/god-eye/events/{event_id}/analysis")
    assert analysis.status_code == 200 and analysis.json()["enrichment_status"] == "not_available"
