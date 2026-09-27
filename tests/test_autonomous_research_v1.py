from datetime import datetime, timezone

from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.storage import GodEyeStore
from nova_api.god_eye.trader import (PaperExecutionEngine, PaperLedger, ReplayEngine, TraderConfig,
    approximate_attribution, missed_outcome, strategy_competition)
from nova_api.god_eye.autonomous_research import (SealedHoldoutGateway, bounded_search, decision_record,
    drift_report, evidence_maturity, feature_ablation, learning_summary)
import pytest

NOW=datetime(2026,1,1,tzinfo=timezone.utc)


def _request(key="one",strategy="NOVA_COMPOSITE"):
    return {"idempotency_key":key,"instrument":"BTC-EUR","side":"buy","quantity":1,"price":100,
            "asset_class":"crypto","venue":"sandbox","strategy":strategy,"opportunity_id":"opp"}


def test_restart_restores_mode_ledger_and_execution_idempotency(tmp_path):
    store=GodEyeStore(tmp_path/"state.sqlite")
    service=GodEyeService(store=store,instruments=[],crypto_providers=[],news_providers=[])
    service.set_trader_mode("AUTO_PAPER")
    fill=service.execution_engine.execute(_request(),available_cash=1000,at=NOW)["fills"][0]
    service.trader.apply_fill(fill); store.save_trader_record("fill",fill["fill_id"],fill["timestamp"],"NOVA_COMPOSITE",fill)
    order=service.execution_engine._orders["one"][0]
    from dataclasses import asdict
    store.save_trader_record("order",order.order_id,order.created_at,"NOVA_COMPOSITE",asdict(order))
    service._persist_trader_state(NOW)
    restarted=GodEyeService(store=GodEyeStore(tmp_path/"state.sqlite"),instruments=[],crypto_providers=[],news_providers=[])
    assert restarted.get_trader_status()["automation_enabled"] is True
    assert restarted.trader.positions
    assert restarted.execution_engine.execute(_request(),available_cash=1000,at=NOW)["duplicate"] is True


def test_replay_attribution_missed_and_strategy_isolation():
    fill=PaperExecutionEngine().execute(_request(),available_cash=1000,at=NOW)["fills"][0]
    replay=ReplayEngine().replay([fill,fill],starting_capital=1000)
    assert replay["unique_fills"]==1 and replay["network_calls"]==0 and replay["deterministic"]
    attribution=approximate_attribution({"uncertainty":.2,"expected_costs":.01,"supporting_patterns":[]},fill)
    assert attribution["causal"] is False and attribution["components"]["costs"]<0
    assert missed_outcome({"action":"WAIT"},100,110,costs=.01)["classification"]=="missed_winner"
    result=strategy_competition([{"strategy":"MOMENTUM","timestamp":"1","realized_pnl":5}],starting_capital=100)
    assert result["strategies"]["MOMENTUM"]["ending_equity"]==105
    assert result["strategies"]["MEAN_REVERSION"]["status"]=="insufficient_point_in_time_data"


def test_ledger_state_round_trip():
    ledger=PaperLedger(TraderConfig(starting_capital=321)); state=ledger.state()
    restored=PaperLedger.restore(state)
    assert restored.cash==321 and restored.config.mode.value=="OBSERVE" and restored.state()["paper_only"]


def test_decision_dataset_rejects_lookahead_and_learns_separately():
    row=decision_record({"regime":"trend"},"EXIT",{"net_result":-2,"error_type":"premature_exit"},
                        decided_at=NOW.isoformat(),resolved_at=NOW.isoformat())
    result=learning_summary([row])
    assert row["look_ahead"] is False and result["decision_quality"]["EXIT"]["sample_count"]==1
    assert result["error_taxonomy"]["premature_exit"]["financial_impact"]==-2
    with pytest.raises(ValueError):decision_record({"forward_return":1},"ENTER",{},decided_at="1",resolved_at="2")


def test_bounded_research_ablation_maturity_drift_and_holdout_boundary():
    rows=bounded_search({"threshold":[.1,.2,.3]},lambda p:{"score":p["threshold"]},max_candidates=2)
    assert len(rows)==2
    with pytest.raises(ValueError):bounded_search({"private_holdout":[1]},lambda p:{},max_candidates=1)
    ablation=feature_ablation({"momentum":1,"events":2},lambda p:{"count":len(p)})
    assert ablation["baseline"]["count"]==2 and ablation["ablations"]["minus:events"]["count"]==1
    assert evidence_maturity(resolved_forecasts=100,resolved_trades=50,duration_days=60,regimes=4,assets=4)["tier"]=="MATURE"
    assert drift_report({"calibration":1},{"calibration":.5})["drift_detected"]
    gateway=SealedHoldoutGateway(maximum_evaluations=1)
    public=gateway.evaluate("v1",{"net_return":.1,"raw_trade":99})
    assert public["raw_rows_exposed"] is False and "raw_trade" not in public["aggregate_metrics"]
    with pytest.raises(RuntimeError):gateway.evaluate("v1",{"net_return":.2})
