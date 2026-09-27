from pathlib import Path

from nova_api.model_usage import ModelUsageStore, normalize_provider_usage


def test_provider_usage_normalization_never_promotes_estimate():
    assert normalize_provider_usage({}) == (None, None, None, "unavailable", False)
    assert normalize_provider_usage({"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}) == (
        12, 4, 16, "provider_reported", True,
    )
    assert normalize_provider_usage({"estimated_input_tokens": 999}) == (
        None, None, None, "unavailable", False,
    )


def test_usage_store_records_fallback_without_prompt_payload(tmp_path: Path):
    store = ModelUsageStore(tmp_path / "usage.sqlite3")
    response = {
        "message": {"content": "secret response body"},
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "_meta": {
            "fallback_reason": "timeout",
            "attempt_history": [
                {"provider": "a", "model": "m1", "result": "error", "reason": "TIMEOUT", "duration_ms": 30},
                {"provider": "b", "model": "m2", "result": "success", "reason": "fallback", "duration_ms": 20},
            ],
        },
    }
    records = store.record_response(response, purpose="planning", conversation_id="c", goal_id="g")
    assert len(records) == 2
    assert records[0].authoritative_usage is False
    assert records[1].fallback_from == "a"
    assert records[1].tokens_total == 15
    assert records[1].authoritative_usage is True
    summary = store.summary(goal_id="g")
    assert summary["model_calls"] == 2
    assert summary["fallbacks"] == 1
    assert summary["authoritative_tokens_total"] == 15
    raw = (tmp_path / "usage.sqlite3").read_bytes()
    assert b"secret response body" not in raw


def test_agent_loop_records_structural_usage_without_prompt(tmp_path):
    from threading import Event
    from nova_api.agent_loop import AgentLoop
    from nova_api.capabilities import build_default_registry
    from nova_api.journal import EventJournal

    class Engine:
        def agent_turn(self, messages, tools, *, timeout_seconds):
            assert messages and timeout_seconds > 0
            return {
                "message": {"role": "assistant", "content": "ok"},
                "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
                "_meta": {"attempt_history": [
                    {"provider": "fake", "model": "tiny", "result": "success", "duration_ms": 4}
                ]},
            }

    journal = EventJournal(tmp_path / "events.sqlite3")
    loop = AgentLoop(Engine(), build_default_registry(journal, project_root=tmp_path), journal)
    outcome = loop.run("bonjour", [], "supervised", Event(), conversation_id="c", generation_id="gen",
                       notify=lambda *_args: None)
    assert outcome.status == "success"
    summary = loop.model_usage.summary()
    assert summary["authoritative_tokens_total"] == 9
    events = journal.recent(limit=20)
    model = next(event for event in events if event.type == "model.attempt")
    assert model.provider == "fake" and model.model == "tiny"
    assert model.input_tokens == 7 and model.output_tokens == 2


def test_usage_summary_exposes_latency_only_with_supported_sample(tmp_path: Path):
    store = ModelUsageStore(tmp_path / "usage.sqlite3")
    for duration in (10, 20):
        store.record_response({
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "_meta": {"attempt_history": [
                {"provider": "p", "model": "m", "result": "success", "duration_ms": duration}
            ]},
        }, purpose="planning")
    provider = store.summary()["providers"]["p::m"]
    assert provider["latency_median_ms"] == 15
    assert "latency_p95_ms" not in provider
