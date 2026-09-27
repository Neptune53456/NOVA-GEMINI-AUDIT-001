from datetime import datetime, timedelta, timezone

import pytest

from nova_api.god_eye.costs import CostEngine
from nova_api.god_eye.star_finder import lifecycle_transition
from nova_api.god_eye.storage import GodEyeStore
from nova_api.god_eye.trader import (LedgerPosition, PaperExecutionEngine, PaperLedger, TraderConfig,
    TraderMode, capital_simulation, performance, position_action, switch_advantage)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def request(**overrides):
    value={"idempotency_key":"k1","instrument":"BTC-EUR","side":"buy","quantity":1,"price":100,
           "asset_class":"crypto","venue":"kraken","strategy":"NOVA_COMPOSITE","opportunity_id":"o1"}
    value.update(overrides); return value


def test_lifecycle_open_closed_resolved_has_audit_refs():
    closed=lifecycle_transition("o1","EXIT","CLOSED",reasons=["full_fill"],at=NOW,trade_ref="t1")
    resolved=lifecycle_transition("o1","CLOSED","RESOLVED",reasons=["outcomes_collected"],at=NOW)
    assert closed["trade_ref"]=="t1" and resolved["to_status"]=="RESOLVED"
    with pytest.raises(ValueError): lifecycle_transition("o1","EXIT","RESOLVED",reasons=[],at=NOW)


def test_execution_prices_partial_fill_costs_and_duplicate_prevention():
    engine=PaperExecutionEngine(CostEngine())
    result=engine.execute(request(),microstructure={"status":"available","spread_bps":10,"ask_depth":.4,"bid_depth":1},available_cash=1000,at=NOW)
    assert result["order"]["status"]=="partially_filled"
    assert result["fills"][0]["execution_price"]>100
    assert result["fills"][0]["fees"]>0 and result["fills"][0]["slippage"]>0
    assert engine.execute(request(),available_cash=1000,at=NOW)["duplicate"] is True


def test_execution_rejections_limit_and_kill_switch():
    assert PaperExecutionEngine().execute(request(),available_cash=10,at=NOW)["order"]["rejection_reason"]=="insufficient_cash"
    assert PaperExecutionEngine().execute(request(),available_cash=1000,kill_switch=True,at=NOW)["order"]["rejection_reason"]=="kill_switch_active"
    pending=PaperExecutionEngine().execute(request(order_type="limit",limit_price=99),available_cash=1000,at=NOW)
    assert pending["order"]["status"]=="pending"


def test_portfolio_accounting_mfe_mae_drawdown_and_close():
    engine=PaperExecutionEngine(); ledger=PaperLedger(TraderConfig(starting_capital=1000))
    buy=engine.execute(request(quantity=2),available_cash=1000,at=NOW)["fills"][0]; ledger.apply_fill(buy)
    high=ledger.mark({"BTC-EUR":120},NOW+timedelta(hours=1)); ledger.mark({"BTC-EUR":90},NOW+timedelta(hours=2))
    sell=engine.execute(request(idempotency_key="k2",side="sell",quantity=2,price=110),available_cash=0,at=NOW+timedelta(hours=3))["fills"][0]
    ledger.apply_fill(sell)
    assert high["unrealized_pnl"]>0 and ledger.closed[0]["mfe"]>0 and ledger.closed[0]["mae"]<0
    assert ledger.public()["fees_paid"]>0


def test_sizing_risk_budget_actions_and_switch_cost():
    ledger=PaperLedger(TraderConfig(starting_capital=100,halt_drawdown=.2)); ledger.peak_equity=100
    ledger.equity_snapshots.append({"equity":75,"drawdown":.25})
    assert ledger.risk_budget()["new_entries_allowed"] is False
    assert ledger.size({"star_score":90,"uncertainty":.1,"expected_net_return":.02})==0
    p=LedgerPosition("p","X","NOVA_COMPOSITE","o",1,100,NOW.isoformat(),"equity",None,"EUR")
    assert position_action(p,{"expected_net_return":0,"action_cost":.001},now=NOW+timedelta(hours=1))["action"]=="EXIT"
    assert switch_advantage({"expected_net_return":.01},{"expected_net_return":.03},exit_cost=.002,entry_cost=.002)["switch"] is True


def test_persistence_performance_and_capital_simulation(tmp_path):
    store=GodEyeStore(tmp_path/"trader.sqlite")
    trade={"timestamp":NOW.isoformat(),"realized_pnl":10,"fees":1,"slippage":2}
    assert store.save_trader_record("trade","t1",trade["timestamp"],"NOVA_COMPOSITE",trade)
    assert not store.save_trader_record("trade","t1",trade["timestamp"],"NOVA_COMPOSITE",trade)
    assert GodEyeStore(tmp_path/"trader.sqlite").trader_records("trade")[0]["realized_pnl"]==10
    simulation=capital_simulation([trade],100); assert simulation["ending_equity"]==110 and simulation["look_ahead"] is False
    assert capital_simulation([],100)["status"]=="insufficient_point_in_time_data"
    assert performance(PaperLedger())["metrics_reliable"] is False


def test_modes_are_paper_only():
    assert {m.value for m in TraderMode}=={"OBSERVE","ASSISTED_PAPER","AUTO_PAPER"}
