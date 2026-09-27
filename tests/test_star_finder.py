from datetime import datetime, timedelta, timezone

import pytest

from nova_api.god_eye.costs import CostConfig, CostEngine, VenueCost
from nova_api.god_eye.market_intelligence import quantitative_divergences
from nova_api.god_eye.scheduler import GodEyeScheduler
from nova_api.god_eye.star_finder import (StarFinderConfig, entry_timing, expected_net_return,
    lifecycle_transition, position_decision, rank_opportunities, rejection_gates, star_score)
from nova_api.god_eye.storage import GodEyeStore

UTC=timezone.utc


def forecast(**updates):
    value={"direction":"up","expected_return_estimate":.03,"probability_up":.65,
        "calibration_sample_count":50,"uncertainty":.2}
    value.update(updates);return value


def opportunity(**updates):
    value={"opportunity_id":"o1","status":"QUALIFIED","expected_net_return":.02,"expected_gross_return":.03,
        "downside":-.01,"uncertainty":.2,"calibrated_probability":.65,"liquidity_quality":.8,
        "intelligence_quality":.8,"regime_stability":.8,"evidence_diversity":.6,"sample_count":50,
        "cost_estimate":{"known":True,"spread_bps":5,"total_round_trip_cost":.005,"notional":1000,"config":{}},
        "rejection_reasons":[],"reference_price":100,"entry_confirmed":True}
    value.update(updates);return value


def test_cost_engine_maker_taker_microstructure_and_execution_prices():
    engine=CostEngine(CostConfig(venue_costs={"x":VenueCost(1,5)}))
    micro={"status":"available","spread_bps":4,"bid_depth":100,"ask_depth":100}
    maker=engine.estimate(asset_class="crypto",venue="x",order_style="maker",size=1000,price=100,microstructure=micro)
    taker=engine.estimate(asset_class="crypto",venue="x",order_style="taker",size=1000,price=100,microstructure=micro)
    assert maker["entry_fee"] < taker["entry_fee"] and maker["spread_bps"]==4
    assert engine.execution_prices(100,"up",maker)["realistic_entry_price"]>100


def test_unknown_cost_degrades_or_rejects_by_config():
    engine=CostEngine(CostConfig(venue_costs={},asset_defaults_bps={},unknown_policy="reject"))
    cost=engine.estimate(asset_class="unknown",venue="none",size=100,price=10)
    assert not cost["known"] and cost["quality"]=="degraded"
    assert "unknown_cost" in rejection_gates(opportunity(cost_estimate=cost),StarFinderConfig(minimum_samples=1))


def test_expected_net_return_and_insufficient_data():
    cost={"total_round_trip_cost":.006}
    value=expected_net_return(forecast(),cost,historical_returns=[-.02,.01,.04])
    assert value["expected_net_return"]==pytest.approx(.024) and value["median_return"]==.01
    assert expected_net_return(forecast(expected_return_estimate=None),cost)["expected_net_return"] is None


def test_star_score_is_bounded_explainable_and_not_probability():
    value=star_score(opportunity())
    assert 0<=value["score"]<=100 and value["components"] and value["is_probability_of_gain"] is False


def test_hard_gates_and_small_capital():
    bad=opportunity(calibrated_probability=None,sample_count=2,liquidity_quality=.1,uncertainty=.9,
        expected_net_return=-.01,stale=True,intelligence_quality=.1,risk_approved=False)
    reasons=rejection_gates(bad,StarFinderConfig(small_capital=True,capital=100,minimum_notional=25))
    assert {"stale_data","calibration_required","risk_engine_rejected","small_capital_minimum_notional"}<=set(reasons)


def test_ranking_and_empty_ranking():
    high=opportunity(opportunity_id="high",star_score=90);low=opportunity(opportunity_id="low",star_score=20)
    assert [v["opportunity_id"] for v in rank_opportunities([low,high])["opportunities"]]==["high","low"]
    assert rank_opportunities([])["status"]=="NO QUALIFIED OPPORTUNITY"


def test_entry_enter_wait_ignore():
    assert entry_timing(opportunity())["action"]=="ENTER"
    assert entry_timing(opportunity(entry_confirmed=False))["action"]=="WAIT"
    assert entry_timing(opportunity(rejection_reasons=["stale_data"]))["action"]=="IGNORE"


def test_position_hold_add_reduce_exit():
    position={"position_id":"p"}
    assert position_decision(position,opportunity())["action"]=="HOLD"
    assert position_decision(position,opportunity(new_independent_evidence=True))["action"]=="ADD"
    assert position_decision(position,opportunity(uncertainty=.8))["action"]=="REDUCE"
    assert position_decision(position,opportunity(thesis_invalidated=True))["action"]=="EXIT"
    assert position_decision(position,opportunity(),risk_approved=False)["paper_only"] is True


def test_quantitative_divergence_is_point_in_time():
    start=datetime(2026,1,1,tzinfo=UTC);rows=[]
    for i in range(30):
        price=100+i if i<20 else 120+(i-20)*.1
        rows.append({"opened_at":(start+timedelta(minutes=i)).isoformat(),"close":price,"volume":200-i,
            "open":price,"high":price+1,"low":price-1,"interval":"1m"})
    values=quantitative_divergences(rows,instrument="X",timeframe="1m",as_of=start+timedelta(minutes=30))
    assert any(v["divergence_type"]=="price_vs_volume" and v["direction"]=="bearish" for v in values)
    assert all(v["strength_is_probability"] is False for v in values)


def test_lifecycle_persistence_is_idempotent_and_restart_safe(tmp_path):
    store=GodEyeStore(tmp_path/"star.sqlite");at=datetime(2026,1,1,tzinfo=UTC)
    detected=lifecycle_transition("o",None,"DETECTED",reasons=["scan"],at=at)
    assert store.save_lifecycle(detected) and not store.save_lifecycle(detected)
    assert GodEyeStore(tmp_path/"star.sqlite").lifecycle("o")[0]["to_status"]=="DETECTED"
    with pytest.raises(ValueError):lifecycle_transition("o","DETECTED","OPEN",reasons=[],at=at)


def test_scheduler_star_task_checkpoint_restart(tmp_path):
    store=GodEyeStore(tmp_path/"scheduler.sqlite");calls=[];now=datetime(2026,1,1,tzinfo=UTC)
    scheduler=GodEyeScheduler(lambda:None,lambda:None,extra_tasks={"star_finder":(lambda:calls.append(1),10)},
        clock=lambda:now,save_state=store.save_scheduler_state)
    assert scheduler.run_due() and calls==[1]
    restarted=GodEyeScheduler(lambda:None,lambda:None,extra_tasks={"star_finder":(lambda:None,10)},
        clock=lambda:now,initial_state=store.scheduler_state())
    assert restarted.status()["tasks"]["star_finder"]["last_status"]=="success"
