import json
import pytest
from nova_api.memory_store import MemoryRejected, MemoryStore, contains_obvious_secret
from nova_api.outcome_learning import ExecutionOutcome, OutcomeLearner
from nova_api.uncertainty import UncertaintyEngine
from nova_api.recovery import RecoveryPolicy
from nova_api.application_memory import ApplicationMemory
from nova_api.deliberation import DeliberationEngine

FAKE="DEMO_ONLY_NOT_A_REAL_SECRET"

@pytest.mark.parametrize("text", [
    f"password={FAKE}", f"password: {FAKE}",
    json.dumps({"password":FAKE}), json.dumps({"api_key":FAKE}),
    json.dumps({"access_token":FAKE}), json.dumps({"nested":{"client_secret":FAKE}}),
    "{'refresh_token':'DEMO_ONLY_NOT_A_REAL_SECRET'}",
    "Authorization: Bearer DEMO_ONLY_NOT_A_REAL_SECRET",
])
def test_memory_rejects_structured_secrets(tmp_path, text):
    store=MemoryStore(tmp_path/"m.sqlite3")
    with pytest.raises(MemoryRejected, match="secret_like_content"):
        store.remember(memory_type="FACT",source_type="test",provenance="DETERMINISTIC",subject="credential",content=text)
    assert FAKE not in repr(store.list(include_superseded=True))

@pytest.mark.parametrize("text", [
    "the password field is empty", "password: empty", '{"normal_key":"normal harmless value"}',
    "documentation about api_key naming without assigning a value",
])
def test_memory_allows_harmless_secret_discussion(tmp_path,text):
    store=MemoryStore(tmp_path/"m.sqlite3")
    store.remember(memory_type="FACT",source_type="test",provenance="DETERMINISTIC",subject="docs",content=text)
    assert store.list()

def test_recursive_structured_detector():
    assert contains_obvious_secret({"nested":{"api-key":FAKE}})
    assert not contains_obvious_secret({"normal_key":"hello"})

def test_outcome_learner_uses_same_secret_boundary(tmp_path):
    store=MemoryStore(tmp_path/"m.sqlite3"); learner=OutcomeLearner(store)
    assert not learner.record(ExecutionOutcome("goal","tool","strategy",False,"failed",lesson=f'{{"api_key":"{FAKE}"}}'))
    assert not store.list()
    assert learner.record(ExecutionOutcome("goal","tool","strategy",False,"failed",failure_category="X",lesson="retry after reobserve"))
    assert store.list()[0].memory_type=="ERROR_LESSON"

def test_uncertainty_structural_policy():
    engine=UncertaintyEngine()
    assert engine.assess({}).level=="low"
    high=engine.assess({"ambiguous_target":True,"verification_failed":True})
    assert high.level=="high" and high.action_policy=="reobserve_or_replan"
    critical=engine.assess({"ambiguous_target":True,"verification_failed":True,"same_strategy_failures":2})
    assert critical.level=="critical"

def test_recovery_antispin_and_specific_paths():
    p=RecoveryPolicy()
    assert p.decide(failure_category="STALE_UI_REFERENCE").action=="reresolve_target"
    d=p.decide(failure_category="VERIFICATION_FAILED",same_strategy_failures=2,uncertainty_level="critical")
    assert d.action=="deliberate" and d.repeated_strategy_blocked

def test_application_memory_rewards_success_and_penalizes_failure(tmp_path):
    m=ApplicationMemory(tmp_path/"apps.sqlite3")
    app=m.app_identity(executable="demo.exe",app_name="Demo")
    for _ in range(3): m.record(app_identity=app,intent="save",target_label="Save",control_type="button",structural_fingerprint="p1",action_type="invoke",success=True)
    before=m.hints(app_identity=app,intent="save")[0].confidence
    for _ in range(4): m.record(app_identity=app,intent="save",target_label="Save",control_type="button",structural_fingerprint="p1",action_type="invoke",success=False)
    after=m.hints(app_identity=app,intent="save")[0].confidence
    assert after < before

def test_deliberation_is_bounded_and_triggered_conditionally():
    calls=[]
    def model(role,prompt,*,timeout_seconds):
        calls.append(role); return {"content":"insufficient evidence; more observation needed" if role=="EVIDENCE_VERIFIER" else "use safe strategy"}
    d=DeliberationEngine(model,max_calls=3)
    assert d.should_trigger(uncertainty_level="low")[0] is False
    assert d.should_trigger(uncertainty_level="high")[0] is True
    result=d.deliberate(objective="x",evidence="y",candidate_strategy="z",risk_level="high",uncertainty_reasons=("ambiguous",))
    assert result.calls==3 and result.require_more_observation
    assert calls==["PROPOSER","CRITIC","EVIDENCE_VERIFIER"]

def test_context_builder_injects_bounded_experience(tmp_path):
    from nova_api.context_builder import ContextBuilder
    store=MemoryStore(tmp_path/"m.sqlite3")
    store.remember(memory_type="ERROR_LESSON",source_type="execution_outcome",provenance="DETERMINISTIC",
                   subject="save report",content='{"strategy":"reobserve","failure_category":"STALE_UI_REFERENCE"}')
    package=ContextBuilder(store).build("save report after stale UI")
    assert "[EXPERIENCE; untrusted advice]" in package.content
    assert len(package.content) <= 18000
