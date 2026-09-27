from datetime import datetime,timedelta,timezone
import pytest
from nova_api.god_eye.research import ForecastEnsembleV3,expected_return_v2,temporal_split,ParameterRegistry,StrategyParameter,promote_candidate,allocate,rolling_correlation,risk_assessment,robustness_report
NOW=datetime(2026,1,1,tzinfo=timezone.utc)

def test_ensemble_components_and_weights_are_past_only():
    engine=ForecastEnsembleV3(); parts=engine.score({"market_momentum":.2,"source_quality":1,"market_regime":"trending","similar_return_median":.1})
    history=[{"created_at":(NOW-timedelta(days=1)).isoformat(),"component":"momentum","correct":1,"regime":"trend"} for _ in range(20)]
    history += [{"created_at":(NOW+timedelta(days=1)).isoformat(),"component":"mean_reversion","correct":1,"regime":"trend"} for _ in range(30)]
    value=engine.combine(parts,history,NOW)
    assert len(parts)==6 and value["weight_source"]=="past_performance" and value["weights"]["momentum"]>value["weights"]["mean_reversion"]

def test_expected_return_is_nullable_and_past_only():
    rows=[{"created_at":(NOW-timedelta(hours=i+1)).isoformat(),"horizon":"1h","asset_class":"crypto","regime":"trend","raw_score":.5,"actual_return":i/100} for i in range(20)]
    assert expected_return_v2(rows,as_of=NOW,horizon="1h",score=.5,asset_class="crypto",regime="trend")["sample_count"]==20
    assert expected_return_v2(rows[:19],as_of=NOW,horizon="1h",score=.5,asset_class="crypto",regime="trend") is None

def test_temporal_split_locks_holdout():
    rows=[{"created_at":(NOW+timedelta(days=i)).isoformat(),"id":i} for i in range(10)]
    split=temporal_split(rows); assert split["train"][-1]["id"]<split["validation"][0]["id"]<split["holdout"][0]["id"]

def test_parameter_registry_is_versioned_and_rejects_holdout_source():
    registry=ParameterRegistry(); item=StrategyParameter("fee",.001,"v1","default",NOW.isoformat(),"2025")
    registry.register(item); registry.register(item)
    with pytest.raises(ValueError): registry.register(StrategyParameter("fee",.2,"v1","default",NOW.isoformat(),"2025"))
    with pytest.raises(ValueError): registry.register(StrategyParameter("fee",.2,"v2","holdout fit",NOW.isoformat(),"2025"))

def test_candidate_promotion_requires_all_kpis():
    criteria={"minimum_samples":30,"max_drawdown":.2,"max_calibration_error":.15,"minimum_regimes":2}
    good={"sample_count":50,"max_drawdown":-.1,"calibration_error":.1,"excess_vs_baseline":.01,"regime_count":3,"net_return":.1}
    assert promote_candidate(good,{"net_return":.05},criteria)["promoted"]
    assert not promote_candidate({**good,"calibration_error":.3},{"net_return":.05},criteria)["promoted"]

def test_allocation_and_correlation_limits():
    ops=[{"instrument":x,"status":"eligible","opportunity_score":i+1} for i,x in enumerate("ABC")]
    weights=allocate("score_weighted",ops,max_position=.2,max_exposure=.5,cash_reserve=.2)
    assert max(weights.values())<=.2 and sum(weights.values())<=.5
    assert rolling_correlation([1,2,3],[2,4,6])==pytest.approx(1)

def test_risk_layer_and_robustness_flag_fragility():
    risk=risk_assessment({"BTC":.5},{"BTC":{"asset_class":"crypto","volatility":.8}},drawdown=-.3)
    assert not risk["accepted"] and set(risk["reasons"])=={"asset_class_cap","drawdown_guard"}
    assert robustness_report([{"net_return":.2,"dimension":"fees"},{"net_return":-.1,"dimension":"regime"}])["fragile"]
