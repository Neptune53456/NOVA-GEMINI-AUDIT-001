from datetime import datetime, timedelta, timezone

import pytest

from nova_api.god_eye.api import build_router
from nova_api.god_eye.forecasting import EnsembleForecastEngine
from nova_api.god_eye.market_intelligence import (MarketScanner, PatternEngine, aggregate_candles,
    classify_regime_v2, fuse_event_evidence, historical_pattern_statistics, intelligence_quality,
    liquidity_metrics, multi_timeframe_context, parse_microstructure, point_in_time,
    quantitative_features, retrieve_similar_situations, signal_decay)
from nova_api.god_eye.models import ForecastFeatureSet
from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.storage import GodEyeStore

UTC=timezone.utc


def candles(count=80, *, interval="1m", jump=False):
    start=datetime(2026,1,1,tzinfo=UTC); result=[]
    for i in range(count):
        close=100+i*.1+(8 if jump and i==count-1 else 0)
        result.append({"opened_at":(start+timedelta(minutes=i)).isoformat(),"interval":interval,
            "open":close-.05,"high":close+.1,"low":close-.1,"close":close,"volume":100+(400 if i==count-1 else i)})
    return result


def test_multi_timeframe_aggregation_uses_only_closed_candles():
    rows=candles(17); cutoff=datetime(2026,1,1,0,16,30,tzinfo=UTC)
    aggregated=aggregate_candles(rows,"5m",as_of=cutoff)
    assert len(aggregated)==3 and aggregated[-1]["source_count"]==5
    assert len(point_in_time(rows,cutoff))==16


def test_features_are_point_in_time_and_pattern_strength_is_not_probability():
    rows=candles(81,jump=True); as_of=datetime(2026,1,1,1,20,tzinfo=UTC)
    before=quantitative_features(rows,instrument="BTC-USD",timeframe="1m",as_of=as_of)
    rows.append({**rows[-1],"opened_at":datetime(2026,1,2,tzinfo=UTC).isoformat(),"close":99999})
    after=quantitative_features(rows,instrument="BTC-USD",timeframe="1m",as_of=as_of)
    assert before==after
    patterns=PatternEngine().detect(before)
    assert patterns and all(0<=p["strength"]<=1 and p["strength_is_probability"] is False for p in patterns)
    assert all(p["invalidation_conditions"] for p in patterns)


def test_scanner_budget_priority_and_cooldown():
    base=quantitative_features(candles(80,jump=True),instrument="A",timeframe="1m",as_of=datetime(2026,1,1,1,20,tzinfo=UTC))
    candidates=[{**base,"instrument":letter,"freshness":1,"liquidity":1,"event_activity":1} for letter in "ABC"]
    scanner=MarketScanner(max_deep_analyses=2,threshold=0,cooldown_seconds=300); now=datetime(2026,1,1,2,tzinfo=UTC)
    first=scanner.scan(candidates,now=now,priority=["C"]); second=scanner.scan(candidates,now=now,priority=["C"])
    third=scanner.scan(candidates,now=now,priority=["C"])
    assert first["scanned"]==3 and len(first["stage_b"])==2 and first["stage_b"][0]["instrument"]=="C"
    assert [x["instrument"] for x in second["stage_b"]]==["B"] and third["stage_b"]==[]
    assert all(x["score_is_recommendation"] is False for x in first["stage_a"])


def test_regime_and_multi_timeframe_conflict():
    up=quantitative_features(candles(80),instrument="A",timeframe="5m",as_of=datetime(2026,1,1,1,20,tzinfo=UTC))
    down={**up,"timeframe":"1d","values":{**up["values"],"ma_ratio":-.02,"momentum_short":-.02}}
    context=multi_timeframe_context({"5m":up,"1d":down})
    assert context["conflict"] and context["short_term_reversal_inside_long_trend"]
    assert classify_regime_v2(up)["version"]=="regime-v2"


def test_microstructure_coinbase_kraken_and_missing_fallback():
    coin=parse_microstructure("coinbase",{"bids":[["99","2"]],"asks":[["101","3"]]},instrument="BTC",observed_at=datetime.now(UTC))
    kraken=parse_microstructure("kraken",{"result":{"XXBTZUSD":{"bids":[["99","1","0"]],"asks":[["100","1","0"]]}}},instrument="BTC",observed_at=datetime.now(UTC))
    missing=parse_microstructure("x",{},instrument="BTC",observed_at=datetime.now(UTC))
    assert coin["spread_bps"]==pytest.approx(200) and kraken["status"]=="available" and missing["status"]=="unavailable"
    assert liquidity_metrics(coin,2)["is_recommendation"] is False


def test_event_fusion_counts_origins_not_reposts():
    now=datetime.now(UTC); evidence=[{"source_id":"x","origin_id":"wire-1","published_at":now,"source_quality":.8} for _ in range(30)]
    fused=fuse_event_evidence(evidence,now=now)
    assert fused["evidence_count"]==30 and fused["independent_origin_count"]==1


def test_market_memory_rejects_future_and_requires_minimum_sample():
    as_of=datetime(2026,1,2,tzinfo=UTC); target={"features":{"x":1},"regime":"range"}
    cases=[{"situation_id":"past","timestamp":datetime(2026,1,1,tzinfo=UTC),"features":{"x":1.1},"regime":"range","subsequent_returns":{"1h":.01}},
           {"situation_id":"future","timestamp":datetime(2026,1,3,tzinfo=UTC),"features":{"x":1},"regime":"range","subsequent_returns":{"1h":9}}]
    result=retrieve_similar_situations(target,cases,as_of=as_of,minimum_samples=2)
    assert [x["situation_id"] for x in result["similar_cases"]]==["past"] and not result["sample_sufficient"]


def test_pattern_statistics_signal_decay_and_quality_boundaries():
    rows=[{"pattern_type":"breakout","timeframe":"5m","horizon":"1h","forward_return":i/100} for i in range(3)]
    stats=historical_pattern_statistics(rows,minimum_samples=4)
    assert stats[0]["mean_forward_return"] is None and signal_decay(rows,minimum_samples=4)[0]["sample_sufficient"] is False
    quality=intelligence_quality(freshness=1,integrity=1,source_diversity=1,sample_count=20,calibration_quality=1,regime_confidence=1,microstructure_available=True,contradictions=False)
    assert quality["quality_score"]==1 and quality["is_probability_of_gain"] is False


def test_forecast_v4_components_and_calibration_boundary():
    f=ForecastFeatureSet("BTC",datetime(2026,1,1,tzinfo=UTC),.01,101,100,.01,(.01,),2,"news",.8,.8,"unclear",0,
        quantitative_features={"price_volume_confirmation":1},patterns=({"pattern_type":"breakout","strength":.8},),
        multi_timeframe_context={"timeframes":{"5m":"bullish","1h":"bullish"}},microstructure={"depth_imbalance":.2})
    forecast=EnsembleForecastEngine().forecast(f,"1h")
    assert forecast.model_version=="3" and forecast.probability_up is None
    assert forecast.feature_snapshot["ensemble"]["ensemble_version"]=="4"
    assert "v4_contributions" in forecast.feature_snapshot["ensemble"]


def test_snapshot_restart_idempotence_and_api_surface(tmp_path):
    store=GodEyeStore(tmp_path/"god.sqlite"); value={"instrument":"BTC","as_of":"2026-01-01T00:00:00+00:00"}
    assert store.save_intelligence("k","BTC",value["as_of"],"market_intelligence",value)
    assert not store.save_intelligence("k","BTC",value["as_of"],"market_intelligence",value)
    restarted=GodEyeStore(tmp_path/"god.sqlite"); assert restarted.intelligence("market_intelligence")==[value]
    service=GodEyeService(store=restarted,crypto_providers=[],news_providers=[],source_configs=[])
    paths={route.path for route in build_router(service).routes}
    assert {"/api/v1/god-eye/scanner","/api/v1/god-eye/patterns","/api/v1/god-eye/pattern-statistics",
            "/api/v1/god-eye/market-memory/{instrument}"}<=paths
