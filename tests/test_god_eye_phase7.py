from datetime import datetime,timedelta,timezone
import pytest
from fastapi.testclient import TestClient
from nova_api.app import create_app
from nova_api.journal import EventJournal
from nova_api.god_eye.alerts import build_alert,confirmation_graph
from nova_api.god_eye.models import InstrumentType,MarketInstrument
from nova_api.god_eye.providers import detect_candle_gaps
from nova_api.god_eye.sandbox import FakeSandboxBroker,PaperExecutionController,SandboxGuardError
from nova_api.god_eye.service import GodEyeService
from nova_api.god_eye.storage import GodEyeStore
NOW=datetime(2026,1,1,tzinfo=timezone.utc)

def test_governance_persistent_immutable_and_migration_idempotent(tmp_path):
 s=GodEyeStore(tmp_path/'g.db'); value={"name":"x","version":"1"}; assert s.save_governance('parameter','x','1',value,NOW.isoformat()); assert not s.save_governance('parameter','x','1',value,NOW.isoformat())
 with pytest.raises(ValueError):s.save_governance('parameter','x','1',{"name":"changed"},NOW.isoformat())
 assert GodEyeStore(tmp_path/'g.db').governance('parameter')==[value]

def test_alert_dedupe_acknowledge_and_not_trade_signal(tmp_path):
 s=GodEyeStore(tmp_path/'g.db'); alert=build_alert('drift','warning','Calibration drift',['m'],NOW)
 assert s.save_alert(alert) and not s.save_alert(alert); assert not alert['trade_signal']; assert s.acknowledge_alert(alert['alert_id'],NOW.isoformat())

def test_confirmation_graph_counts_independent_sources():
 graph=confirmation_graph([{"event_id":"e","source_ids":["a","b"],"entities":["BTC"]}],[{"post_id":"p","author":{"author_id":"c"}}])
 assert graph['independent_source_count']==3 and len(graph['edges'])==4

def test_gap_detection_is_bounded_and_order_independent():
 candles=[{"opened_at":NOW+timedelta(hours=i*3)} for i in range(30)]
 assert len(detect_candle_gaps(list(reversed(candles)),'1h',max_gaps=4))==4

def test_sandbox_disabled_live_refused_and_kill_switch():
 with pytest.raises(SandboxGuardError):FakeSandboxBroker(enabled=False).submit_order({"notional":1,"idempotency_key":"x"})
 with pytest.raises(SandboxGuardError):FakeSandboxBroker(sandbox=False,enabled=True,kill_switch=False).submit_order({"notional":1,"idempotency_key":"x"})
 with pytest.raises(SandboxGuardError):FakeSandboxBroker(enabled=True,kill_switch=True).submit_order({"notional":1,"idempotency_key":"x"})

def test_sandbox_orders_are_idempotent_and_capped():
 broker=FakeSandboxBroker(enabled=True,kill_switch=False,max_notional=100); request={"symbol":"BTC","side":"up","notional":50,"idempotency_key":"k"}
 assert broker.submit_order(request)==broker.submit_order(request) and len(broker.orders)==1
 with pytest.raises(SandboxGuardError):broker.submit_order({**request,"notional":101,"idempotency_key":"z"})

def test_execution_controller_risk_rejection_and_reconciliation(tmp_path):
 broker=FakeSandboxBroker(enabled=True,kill_switch=False); controller=PaperExecutionController(broker,GodEyeStore(tmp_path/'g.db'))
 opportunity={"instrument":"BTC","direction":"up","status":"eligible","calibration_sample_count":30}
 assert controller.execute(opportunity,{"accepted":False},price_fresh=True,notional=10,idempotency_key='x')['status']=='rejected'
 order=controller.execute(opportunity,{"accepted":True},price_fresh=True,notional=10,idempotency_key='y'); assert controller.reconcile(order['order_id'])['status']=='filled'

def test_pipeline_v3_is_active_and_continuous_job_is_idempotent(tmp_path):
 service=GodEyeService(store=GodEyeStore(tmp_path/'g.db'),instruments=[MarketInstrument('X','X',InstrumentType.EQUITY)],news_providers=[],crypto_providers=[],social_providers=[],llm_enabled=False)
 assert service.forecast_engine.model_version=='3' and 'evaluation' in service.scheduler.tasks
 assert service.continuous_evaluation()['status']=='success' and service.continuous_evaluation()['forecasts_resolved']==0

def test_phase7_read_apis(tmp_path):
 service=GodEyeService(store=GodEyeStore(tmp_path/'g.db'),instruments=[],news_providers=[],crypto_providers=[],social_providers=[],llm_enabled=False)
 client=TestClient(create_app(journal=EventJournal(tmp_path/'j.db'),god_eye_service=service))
 for path in ('alerts','governance','influence-graph'):
  assert client.get('/api/v1/god-eye/'+path).status_code==200
