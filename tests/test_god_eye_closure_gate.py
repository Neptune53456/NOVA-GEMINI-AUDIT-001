from datetime import datetime, timedelta, timezone

from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.storage import GodEyeStore
from nova_api.god_eye.trader import ReplayEngine
from nova_api.god_eye.research_lab import ResearchEngine, ResearchExperiment
from self_improvement.god_eye_benchmark import judge_god_eye_candidate


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def opportunity(**changes):
    value = {"opportunity_id":"closure-opp","instrument":"BTC-EUR","asset_class":"crypto",
        "venue":"sandbox","detected_at":NOW.isoformat(),"status":"QUALIFIED","direction":"up",
        "reference_price":100.0,"star_score":90,"uncertainty":.1,"expected_net_return":.05,
        "expected_costs":.001,"action_cost":.001,"horizon":"1h","model_version":"v1","paper_only":True}
    value.update(changes); return value


def history(*prices):
    return [{"opened_at":(NOW+timedelta(hours=i)).isoformat(),"close":price} for i,price in enumerate(prices)]


def service(tmp_path):
    result=GodEyeService(store=GodEyeStore(tmp_path/"closure.sqlite"),instruments=[],crypto_providers=[],
        news_providers=[],source_configs=[],social_providers=[])
    result._best_history=lambda instrument: history(100,110,108)
    result.set_trader_mode("AUTO_PAPER")
    return result


def test_auto_paper_entry_exit_restart_resolution_and_replay(tmp_path):
    app=service(tmp_path); item=opportunity(); app.store.save_star_opportunity(item)
    app._auto_paper([item],NOW)
    assert app.trader.positions
    assert app.get_portfolio() == app.get_trader_portfolio()
    midpoint=GodEyeService(store=GodEyeStore(tmp_path/"closure.sqlite"),instruments=[],crypto_providers=[],
        news_providers=[],source_configs=[],social_providers=[])
    assert midpoint.get_portfolio() == app.get_trader_portfolio()
    exiting=opportunity(expected_net_return=0); app.store.save_star_opportunity(exiting)
    result=app.reevaluate_paper_positions(NOW+timedelta(hours=1))
    assert result["actions"][0]["decision"]["action"]=="EXIT" and not app.trader.positions
    restarted=GodEyeService(store=GodEyeStore(tmp_path/"closure.sqlite"),instruments=[],crypto_providers=[],
        news_providers=[],source_configs=[],social_providers=[])
    restarted._best_history=lambda instrument: history(100,110,108)
    resolved=restarted.resolve_due_outcomes(NOW+timedelta(hours=3))
    assert resolved["resolved"]==1
    assert [v["to_status"] for v in restarted.store.lifecycle("closure-opp")][-2:]==["CLOSED","RESOLVED"]
    replay=restarted.replay_trader()
    assert replay["status"]=="ok" and replay["network_calls"]==replay["production_writes"]==0
    experiment=ResearchExperiment("closure-campaign","improve paper threshold",{},"train","validation","sealed",{})
    challenger=ResearchEngine(restarted.store,budget=1,seed=7).run(experiment,{"threshold":[.2]},
        lambda params,split:{"split":split,"net_return":.2})[0]
    incumbent={"benchmark_score":90,"calibration_error":.1,"max_drawdown":-.1,"latency_ms":10,"ingestion_reliability":.99}
    candidate={**incumbent,"benchmark_score":91,"tests_passed":True,"validation_passed":True,
        "holdout_passed":True,"robustness_passed":True}
    assert challenger["validation_metrics"]["split"]=="validation"
    assert judge_god_eye_candidate(candidate,incumbent)["decision"]=="ACCEPT"


def test_partial_reduce_is_idempotent_and_keeps_position_open(tmp_path):
    app=service(tmp_path); item=opportunity(); app.store.save_star_opportunity(item); app._auto_paper([item],NOW)
    initial=next(iter(app.trader.positions.values())).quantity
    app.store.save_star_opportunity(opportunity(uncertainty=.8))
    first=app.reevaluate_paper_positions(NOW+timedelta(hours=1)); after=next(iter(app.trader.positions.values())).quantity
    assert first["actions"][0]["decision"]["action"]=="REDUCE" and abs(after)==abs(initial)/2
    duplicate=app.reevaluate_paper_positions(NOW+timedelta(hours=1))
    assert duplicate["actions"][0]["decision"]["action"]=="HOLD"


def test_continuous_ledgers_are_isolated_and_compete(tmp_path):
    app=service(tmp_path); item=opportunity(); app.store.save_star_opportunity(item); app._auto_paper([item],NOW)
    assert all(ledger.fills for ledger in app.strategy_ledgers.values())
    assert len({id(v) for v in app.strategy_ledgers.values()})==len(app.strategy_ledgers)
    app.store.save_star_opportunity(opportunity(expected_net_return=0)); app.reevaluate_paper_positions(NOW+timedelta(hours=1))
    result=app.get_strategy_competition()
    assert all(result["strategies"][name]["status"]=="ok" for name in app.strategy_ledgers)


def test_replay_v2_fails_closed_when_snapshot_chain_is_incomplete():
    result=ReplayEngine().replay([{"kind":"fill","payload":{"fill_id":"x"}}])
    assert result["status"]=="insufficient_replay_data" and "forecast" in result["missing_stages"]


def test_scheduler_and_watchdog_cover_closure_jobs(tmp_path):
    app=service(tmp_path); tasks=app.scheduler.status()["tasks"]
    assert {"paper_positions","outcome_resolution"}<=set(tasks)
    health=app.get_endurance_health()
    assert "closed_trades_not_resolved" in health["backlogs"] and health["watchdog"]["bounded_recovery"]
    assert health["outcome_state"] == {"pending": 0, "resolved": 0, "retry_pending": 0, "exhausted": 0}


def test_fresh_paper_portfolio_cash_and_idle_ledgers(tmp_path):
    app=GodEyeService(store=GodEyeStore(tmp_path/"fresh.sqlite"),instruments=[],crypto_providers=[],
        news_providers=[],source_configs=[],social_providers=[])
    portfolio=app.get_portfolio()
    assert portfolio == app.get_trader_portfolio()
    assert portfolio["equity"] == portfolio["cash"] == 10000
    assert portfolio["positions"] == []
    health=app.get_endurance_health()
    assert health["backlogs"]["strategy_ledgers_stalled"] == 0
    assert health["watchdog"]["healthy"] is True
    app.trader.fills.append({"fill_id":"unmirrored"})
    assert app.get_endurance_health()["backlogs"]["strategy_ledgers_stalled"] == 5
