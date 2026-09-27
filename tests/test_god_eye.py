from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from nova_api.app import create_app
from nova_api.god_eye.config import DEFAULT_NEWS_SOURCES
from nova_api.god_eye.intelligence import EventIntelligence, classify_event, match_entities
from nova_api.god_eye.models import (DataQuality, InstrumentType, MarketCandle, MarketInstrument,
                                     MarketQuote, NewsItem, NewsSourceConfig, SourceKind,
                                     SourceMetadata, as_utc, public)
from nova_api.god_eye.scheduler import GodEyeScheduler
from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.storage import GodEyeStore
from nova_api.god_eye.providers import RssAtomProvider
from nova_api.journal import EventJournal


INSTRUMENT = MarketInstrument("BTC-USD", "Bitcoin", InstrumentType.CRYPTO,
                              provider_symbols={"kraken": "XBTUSD"}, entities=("Bitcoin",))


def make_quote(provider="test", *, age=0):
    observed = datetime.now(timezone.utc) - timedelta(seconds=age)
    source = SourceMetadata(provider, f"https://{provider}.example/quote", datetime.now(timezone.utc))
    return MarketQuote(INSTRUMENT, 100.5, observed, source,
                       DataQuality(observed, source.retrieved_at, 180))


class Provider:
    def __init__(self, name, result=None, error=None, candles=None):
        self.name, self.result, self.error, self.calls, self.candle_values = name, result, error, 0, candles

    def quote(self, instrument):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    def candles(self, instrument, interval, limit):
        if self.error:
            raise self.error
        return self.candle_values or []


class NewsProvider:
    name = "feed"
    def __init__(self, items=None, error=None):
        self.items, self.error = items or [], error

    def fetch(self, instruments):
        if self.error:
            raise self.error
        return self.items


class FeedClient:
    def get_bytes(self, url, *, accept):
        return (b"<rss><channel><item><title>Bitcoin regulation</title>"
                b"<link>https://news.example/a</link><pubDate>Tue, 23 Sep 2026 12:00:00 GMT</pubDate>"
                b"<description>Bitcoin crypto update</description></item></channel></rss>",
                "application/rss+xml", url)


def service(tmp_path, **kwargs):
    return GodEyeService(store=GodEyeStore(tmp_path / "god.sqlite3"), instruments=[INSTRUMENT], **kwargs)


def test_market_normalization_uses_utc_and_exposes_freshness():
    assert as_utc("2026-01-02T03:04:05Z").tzinfo == timezone.utc
    value = public(make_quote(age=181))
    assert value["observed_at"].endswith("+00:00")
    assert value["quality"]["stale"] is True


def test_market_snapshot_distinguishes_closed_session_from_stale(tmp_path, monkeypatch):
    equity = MarketInstrument("AAPL", "Apple", InstrumentType.EQUITY)
    crypto = MarketInstrument("BTC-USD", "Bitcoin", InstrumentType.CRYPTO)
    target = GodEyeService(store=GodEyeStore(tmp_path / "freshness.sqlite3"), instruments=[equity, crypto],
                           crypto_providers=[], yahoo_provider=Provider("yahoo"))
    old = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
    target._cache(MarketQuote(equity, 100, old, SourceMetadata("yahoo", "https://example.test", old), DataQuality(old, old, 900)))
    target._cache(MarketQuote(crypto, 100, old, SourceMetadata("coinbase", "https://example.test", old), DataQuality(old, old, 180)))
    monkeypatch.setattr("nova_api.god_eye.service.utc_now", lambda: datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc))

    quotes = {item["instrument"]["symbol"]: item for item in target.get_market_snapshot()["quotes"]}

    assert quotes["AAPL"]["quality"]["freshness_status"] == "MARKET_CLOSED"
    assert quotes["AAPL"]["quality"]["stale"] is True
    assert quotes["BTC-USD"]["quality"]["freshness_status"] == "STALE"


def test_market_snapshot_keeps_provider_failure_distinct_from_session_state(tmp_path, monkeypatch):
    equity = MarketInstrument("AAPL", "Apple", InstrumentType.EQUITY)
    target = GodEyeService(store=GodEyeStore(tmp_path / "provider-health.sqlite3"), instruments=[equity],
                           crypto_providers=[], yahoo_provider=Provider("yahoo"))
    old = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
    target._cache(MarketQuote(equity, 100, old, SourceMetadata("yahoo", "https://example.test", old),
                              DataQuality(old, old, 900)))
    target._last_errors["yahoo"] = "NETWORK"
    monkeypatch.setattr("nova_api.god_eye.service.utc_now", lambda: datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc))

    health = target.get_data_health()

    assert health["provider_errors"] == {"yahoo": "NETWORK"}
    assert health["provider_unavailable_quotes"] == 1
    assert health["market_closed_quotes"] == health["stale_quotes"] == 0


def test_crypto_provider_fallback_and_persistence(tmp_path):
    primary = Provider("coinbase", error=RuntimeError("down"))
    fallback = Provider("kraken", result=make_quote("kraken"))
    target = service(tmp_path, crypto_providers=[primary, fallback])

    result = target.refresh_market()

    assert result["status"] == "partial"
    assert result["updated"] == 1
    assert primary.calls == fallback.calls == 1
    assert GodEyeStore(tmp_path / "god.sqlite3").recent_quotes()[0]["source"]["provider"] == "kraken"


def test_news_deduplication_is_deterministic_and_durable(tmp_path):
    now = datetime.now(timezone.utc)
    source = SourceMetadata("feed", "https://news.example/rss", now, "rss_atom")
    first = NewsItem(NewsItem.stable_id("https://news.example/a", "A"), "A", "https://news.example/a",
                     now, "Bitcoin update", source, ("BTC-USD",), ("Bitcoin",), ("crypto",))
    duplicate = NewsItem(NewsItem.stable_id("https://news.example/a/", "Changed"), "Changed",
                         "https://news.example/a/", now, "same", source)
    target = service(tmp_path, news_providers=[NewsProvider([first, duplicate])])

    result = target.refresh_news()
    second = target.refresh_news()

    assert result["fetched"] == 1 and result["inserted"] == 1
    assert second["inserted"] == 0
    assert len(target.get_recent_news()["items"]) == 1


def test_rss_leaf_elements_and_classification_are_normalized():
    provider = RssAtomProvider("feed", "https://news.example/rss", {"news.example"}, client=FeedClient())
    item = provider.fetch([INSTRUMENT])[0]
    assert item.title == "Bitcoin regulation"
    assert item.entities == ("Bitcoin",)
    assert item.themes == ("regulation", "crypto")


def test_provider_failure_is_isolated_and_reported(tmp_path):
    target = service(tmp_path, crypto_providers=[Provider("one", error=OSError()),
                                                 Provider("two", error=ValueError())])
    result = target.refresh_market()
    assert result["status"] == "error"
    assert target.get_data_health()["status"] == "degraded"


def test_candles_are_normalized_idempotent_and_retained(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    source = SourceMetadata("coinbase", "https://coinbase.example/candles", now)
    candles = [MarketCandle(INSTRUMENT, "1m", now - timedelta(minutes=i), 1, 3, 0.5, 2, 10,
                            source, DataQuality(now - timedelta(minutes=i), now, 120)) for i in range(3)]
    target = service(tmp_path, crypto_providers=[Provider("coinbase", candles=candles)],
                     candle_intervals=("1m",), candle_retention=2)
    assert target.refresh_history()["inserted"] == 3
    assert target.refresh_history()["inserted"] == 1  # pruned oldest record is reinserted then pruned
    history = target.get_history("BTC-USD", "1m")["candles"]
    assert len(history) == 2 and all(item["interval"] == "1m" for item in history)


def test_source_config_is_structured_and_official():
    assert len(DEFAULT_NEWS_SOURCES) >= 5
    assert all(source.url.startswith("https://") and source.allowed_domains for source in DEFAULT_NEWS_SOURCES)
    assert any(source.kind == SourceKind.PRIMARY and source.official for source in DEFAULT_NEWS_SOURCES)


def test_event_classification_entity_mapping_and_source_priority():
    now = datetime.now(timezone.utc)
    low = NewsSourceConfig("low", "Low", "https://low.example/rss", "rss", 0.4,
                           ("low.example",), SourceKind.AGGREGATOR)
    official = NewsSourceConfig("official", "Official", "https://official.example/rss", "rss", 0.95,
                                ("official.example",), SourceKind.PRIMARY, True)
    low_item = NewsItem("n1", "Bitcoin quarterly results", "https://low.example/1", now,
                        "Bitcoin revenue", SourceMetadata("low", low.url, now))
    official_item = NewsItem("n2", "Bitcoin quarterly results confirmed", "https://official.example/2", now,
                             "Bitcoin revenue", SourceMetadata("official", official.url, now))
    engine = EventIntelligence([INSTRUMENT], {"low": low, "official": official})
    events = engine.build([low_item, official_item])
    assert classify_event(low_item) == "earnings/results"
    assert match_entities(low_item, [INSTRUMENT]) == (("Bitcoin",), ("BTC-USD",))
    assert len(events) == 1
    assert events[0].source_ids[0] == "official"
    assert events[0].evidence_refs == ("n2", "n1")


def test_event_fusion_persists_all_evidence_and_novelty(tmp_path):
    now = datetime.now(timezone.utc)
    config = NewsSourceConfig("official", "Official", "https://official.example/rss", "rss", 0.9,
                              ("official.example",), SourceKind.PRIMARY, True)
    engine = EventIntelligence([INSTRUMENT], {"official": config})
    one = NewsItem("one", "Bitcoin regulation announced", "https://official.example/1", now, "rule",
                   SourceMetadata("official", config.url, now))
    store = GodEyeStore(tmp_path / "events.sqlite3")
    first = engine.build([one])
    store.save_events(first)
    two = NewsItem("two", "Bitcoin regulation updated", "https://official.example/2", now, "rule",
                   SourceMetadata("official", config.url, now + timedelta(minutes=1)))
    store.save_events(engine.build([two], store.events()))
    event = store.events()[0]
    assert set(event["evidence_refs"]) == {"one", "two"}
    assert event["novelty_score"] == 0.0


def test_scheduler_backoff_and_no_overlapping_run():
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    calls = []
    scheduler = GodEyeScheduler(lambda: (_ for _ in ()).throw(OSError()),
                                lambda: calls.append("news") or {"status": "success"},
                                market_interval=10, news_interval=20, clock=lambda: now[0])
    assert scheduler.run_due() is True
    assert scheduler.tasks["market"].failures == 1
    assert scheduler.tasks["market"].next_run_at == now[0] + timedelta(seconds=20)
    scheduler._run_lock.acquire()
    try:
        assert scheduler.run_due() is False
    finally:
        scheduler._run_lock.release()
    assert calls == ["news"]


def test_god_eye_api_uses_injected_service(tmp_path):
    target = service(tmp_path, crypto_providers=[Provider("test", result=make_quote())])
    journal = EventJournal(tmp_path / "events.sqlite3")
    client = TestClient(create_app(journal=journal, god_eye_service=target))

    refresh = client.post("/api/v1/god-eye/refresh", json={"market": True, "news": False, "history": False})
    market = client.get("/api/v1/god-eye/market")
    health = client.get("/api/v1/god-eye/health")
    news = client.get("/api/v1/god-eye/news?limit=10")
    events = client.get("/api/v1/god-eye/events")
    missing = client.get("/api/v1/god-eye/events/missing")
    history = client.get("/api/v1/god-eye/history/BTC-USD?interval=1m")
    scheduler = client.get("/api/v1/god-eye/scheduler")

    assert refresh.status_code == 200 and refresh.json()["market"]["updated"] == 1
    assert market.status_code == 200 and len(market.json()["quotes"]) == 1
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert news.status_code == 200 and news.json() == {"items": []}
    assert events.status_code == 200 and events.json() == {"events": []}
    assert missing.status_code == 404
    assert history.status_code == 200 and history.json()["candles"] == []
    assert scheduler.status_code == 200 and scheduler.json()["running"] is False
    assert health.json()["db_status"] == "ok" and "latest_market_timestamp" in health.json()
