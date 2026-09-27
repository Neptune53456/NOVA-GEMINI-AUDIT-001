from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from nova_api.app import create_app
from nova_api.god_eye.config import calibration_quality_tier
from nova_api.god_eye.intelligence import EventIntelligence
from nova_api.god_eye.models import (DataQuality, InstrumentType, MarketInstrument, MarketQuote,
    NewsItem, NewsSourceConfig, SourceKind, SourceMetadata)
from nova_api.god_eye.portfolio import PaperConfig, PaperPortfolio, benchmark_comparison, portfolio_metrics, walk_forward
from nova_api.god_eye.providers import RateLimited, ResilientProviderChain
from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.social import BlueskyProvider, SocialAuthor, SocialPost, SocialSignalDetector
from nova_api.god_eye.storage import GodEyeStore
from nova_api.journal import EventJournal

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ASSET = MarketInstrument("BTC-USD", "Bitcoin", InstrumentType.CRYPTO, entities=("Bitcoin",))


def opportunity(identifier="o1", **overrides):
    value = {"opportunity_id": identifier, "instrument": "BTC-USD", "horizon": "1h", "direction": "up",
             "status": "eligible", "calibration_sample_count": 50, "expected_value_net": .02,
             "uncertainty": .2, "data_quality": .9, "created_at": NOW.isoformat()}
    value.update(overrides); return value


def test_provider_fallback_and_rate_limit_backoff():
    class Bad:
        name = "bad"
        def quote(self, _): raise RateLimited("429")
    class Good:
        name = "good"
        def quote(self, instrument):
            source = SourceMetadata("good", "https://example.test", NOW)
            return MarketQuote(instrument, 100, NOW, source, DataQuality(NOW, NOW, 10**9))
    delays = []
    chain = ResilientProviderChain([Bad(), Good()], retries=1, sleep=delays.append)
    assert chain.call("quote", ASSET).price == 100
    assert delays == [.1] and chain.metrics["bad"]["rate_limited"] == 2


def test_bluesky_parsing_without_scraping():
    class Client:
        def get_json(self, _):
            return ({"posts": [{"uri": "at://did/app.bsky.feed.post/1", "cid": "c", "record": {"text": "Bitcoin update", "createdAt": NOW.isoformat()},
                    "author": {"did": "did:x", "handle": "market.test", "displayName": "Market"}, "likeCount": 4}]}, "https://public.api.bsky.app")
    posts = BlueskyProvider(("bitcoin",), Client()).fetch([ASSET])
    assert len(posts) == 1 and posts[0].instruments == ("BTC-USD",) and posts[0].engagement["likes"] == 4


def test_social_duplicate_burst_and_official_weight():
    author = SocialAuthor("a", "Official", official=True)
    posts = [SocialPost(str(i), "bluesky", author, NOW + timedelta(minutes=i), "Bitcoin breaks out", f"u{i}",
                        ("Bitcoin",), ("BTC-USD",), {"likes": 200}) for i in range(3)]
    signals = SocialSignalDetector().detect(posts)
    assert len(signals) == 1 and signals[0].score >= .7 and signals[0].metrics["duplicate_count"] == 2


def test_event_merge_keeps_news_and_social_evidence_distinct():
    cfg = NewsSourceConfig("official", "Official", "https://x.test", "rss", .9, ("x.test",), SourceKind.PRIMARY, True)
    news = NewsItem("n", "Bitcoin regulation", "https://x.test/n", NOW, "Bitcoin", SourceMetadata("official", "https://x.test", NOW), ("BTC-USD",))
    post = SocialPost("p", "bluesky", SocialAuthor("a", "A"), NOW, "Bitcoin regulation", "u", instruments=("BTC-USD",))
    signal = SocialSignalDetector().detect([post])[0]
    event = EventIntelligence([ASSET], {"official": cfg}).build([news], social_signals=[signal])[0]
    assert {e["type"] for e in event.evidence} == {"news", "social"}


def test_calibration_quality_tiers():
    assert [calibration_quality_tier(n) for n in (19, 20, 50, 200)] == ["insufficient", "low", "medium", "high"]


def test_paper_accounting_fees_slippage_and_idempotence():
    portfolio = PaperPortfolio(PaperConfig(fee_bps=10, slippage_bps=10))
    opened = portfolio.open(opportunity(), 100, NOW)
    assert opened["status"] == "opened"
    assert portfolio.open(opportunity(), 100, NOW)["status"] == "rejected"
    trade = portfolio.close(opened["position"]["position_id"], 110, NOW + timedelta(hours=1))
    assert trade and trade.fees > 0 and trade.slippage > 0 and trade.realized_pnl > 0


def test_paper_exposure_limit_and_metrics_masking():
    portfolio = PaperPortfolio(PaperConfig(max_gross_exposure=.05, max_position_fraction=.1))
    assert "exposure_limit" in portfolio.open(opportunity(), 100, NOW)["reasons"]
    assert portfolio_metrics(portfolio)["sharpe"] is None


def test_walk_forward_uses_first_price_after_horizon_and_benchmarks():
    prices = {"BTC-USD": [(NOW-timedelta(hours=1), 100), (NOW+timedelta(minutes=30), 1000), (NOW+timedelta(hours=1), 110)]}
    result = walk_forward([opportunity()], prices)
    assert result["look_ahead"] is False and result["metrics"]["trade_count"] == 1
    assert set(result["benchmarks"]) >= {"no_trade", "buy_and_hold", "momentum", "random_direction"}
    assert benchmark_comparison(prices["BTC-USD"], 0, seed=7) == benchmark_comparison(prices["BTC-USD"], 0, seed=7)


def test_phase5_storage_and_apis(tmp_path):
    store = GodEyeStore(tmp_path / "god.sqlite3")
    service = GodEyeService(store=store, instruments=[ASSET], news_providers=[], crypto_providers=[], social_providers=[], llm_enabled=False)
    client = TestClient(create_app(journal=EventJournal(tmp_path / "journal.sqlite3"), god_eye_service=service))
    for path in ("social", "social/signals", "portfolio", "portfolio/trades", "walk-forward", "benchmarks"):
        assert client.get(f"/api/v1/god-eye/{path}").status_code == 200
    post = SocialPost("p", "bluesky", SocialAuthor("a", "A"), NOW, "Bitcoin", "u")
    assert store.save_social_posts([post]) == 1 and store.save_social_posts([post]) == 0
