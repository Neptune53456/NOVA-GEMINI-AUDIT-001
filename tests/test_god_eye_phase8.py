from datetime import datetime,timedelta,timezone
import pytest
from fastapi.testclient import TestClient
from nova_api.app import create_app
from nova_api.journal import EventJournal
from nova_api.god_eye.alternative import normalize_macro_event,normalize_sec_filing
from nova_api.god_eye.integrity import assess_candles,GapRepair
from nova_api.god_eye.models import DataQuality,InstrumentType,MarketCandle,MarketInstrument,SourceMetadata
from nova_api.god_eye.research_lab import ResearchEngine,ResearchExperiment,anti_overfit_gate,bounded_bootstrap,PromotionRegistry,live_forward_record
from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.storage import GodEyeStore
from self_improvement.god_eye_benchmark import run_god_eye_benchmark,validate_change_scope
NOW=datetime(2026,1,1,tzinfo=timezone.utc); ASSET=MarketInstrument('X','X',InstrumentType.EQUITY)

def candle(hour,close=100):return {"opened_at":(NOW+timedelta(hours=hour)).isoformat(),"interval":"1h","open":close,"high":close+1,"low":close-1,"close":close,"volume":1}
def test_integrity_marks_not_deletes_suspect_data():
 rows=[candle(3),candle(0),candle(0),candle(1,1000)]; result=assess_candles(rows,'1h')
 assert result['data_quality_score']<1 and {i['issue'] for i in result['issues']}>={'duplicate','out_of_order','impossible_jump'} and len(rows)==4

def test_gap_repair_is_bounded_checkpointed_and_idempotent(tmp_path):
 store=GodEyeStore(tmp_path/'g.db'); source=SourceMetadata('p','https://x',NOW); quality=DataQuality(NOW,NOW,999999)
 store.save_candles([MarketCandle(ASSET,'1h',NOW,1,1,1,1,1,source,quality),MarketCandle(ASSET,'1h',NOW+timedelta(hours=5),1,1,1,1,1,source,quality)])
 class P:
  def __init__(self):self.calls=0
  def candles(self,*args):self.calls+=1;return []
 p=P(); result=GapRepair(store,max_calls=1).run(ASSET,'1h',p)
 assert result['calls']==1 and p.calls==1 and len(store.backfill_checkpoints())==1

def test_filings_and_macro_normalization_never_invent_consensus():
 filing=normalize_sec_filing({"accessionNumber":"1-2","form":"8-K","filingDate":"2026-01-01","cik":"1"},'X')
 macro=normalize_macro_event({"scheduled_at":NOW.isoformat(),"category":"CPI","actual":2.1,"source":"official"})
 assert filing.payload['form']=='8-K' and macro.payload['consensus'] is None and macro.payload['surprise'] is None

def test_research_budget_determinism_isolation_and_holdout_guard(tmp_path):
 store=GodEyeStore(tmp_path/'g.db'); exp=ResearchExperiment('e','h',{},'train','validation','locked',{})
 engine=ResearchEngine(store,budget=2,seed=7); values=engine.run(exp,{"weight":[.1,.2,.3]},lambda p,split:{"return":p['weight'],"split":split})
 assert len(values)==2 and all(v['validation_metrics']['split']=='validation' for v in values)
 with pytest.raises(ValueError):ResearchEngine(store).run(ResearchExperiment('x','h',{},'t','v','h',{}),{"holdout_weight":[1]},lambda p,s:{})

def test_overfit_gate_bootstrap_and_small_sample():
 fragile={"sample_count":100,"train_return":.5,"validation_return":.1,"holdout_return":.1,"parameter_sensitivity":.1,"worst_regime_return":.1,"worst_asset_return":.1,"high_cost_return":.1}
 assert not anti_overfit_gate(fragile)['accepted'];assert bounded_bootstrap([.1]*29)['confidence_interval'] is None;assert bounded_bootstrap([.1]*30)['confidence_interval'] is not None

def test_live_forward_separation_promotion_persistence_and_rollback(tmp_path):
 store=GodEyeStore(tmp_path/'g.db'); record=live_forward_record('v2',NOW.isoformat());assert record['mode']=='live_forward_paper' and not record['historical_backtest']
 registry=PromotionRegistry(store); good={"version":"v2","sample_count":50,"max_drawdown":-.1,"calibration_error":.1,"excess_vs_baseline":.1,"regime_count":3,"net_return":.2}
 assert registry.promote(good,{"version":"v1","net_return":.1},{"minimum_samples":30,"max_drawdown":.2,"max_calibration_error":.2,"minimum_regimes":2},'1')['decision']['promoted']
 assert registry.rollback('v2','v1','drift')['rollback_target']=='v1'

def test_self_improvement_gate_is_validation_only_and_forbids_safety_mutation():
 report=run_god_eye_benchmark({k:True for k in ('ingestion','events','forecasting','calibration','opportunity','portfolio','risk','walk_forward','no_look_ahead')})
 assert report['score']==100 and report['split']=='validation'
 assert not validate_change_scope(['nova_api/god_eye/x.py'],'disable kill_switch')['accepted']
 assert not validate_change_scope(['system_actions.py'],'safe')['accepted']

def test_phase8_apis(tmp_path):
 service=GodEyeService(store=GodEyeStore(tmp_path/'g.db'),instruments=[],news_providers=[],crypto_providers=[],social_providers=[],llm_enabled=False)
 client=TestClient(create_app(journal=EventJournal(tmp_path/'j.db'),god_eye_service=service))
 for path in ('experiments','candidates','incumbent','live-forward','data-integrity','alternative-data/health'):assert client.get('/api/v1/god-eye/'+path).status_code==200
