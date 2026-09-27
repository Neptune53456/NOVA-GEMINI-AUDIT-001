import json
from time import sleep

import pytest

from nova_api.agent_loop import ConfirmationStore
from nova_api.autonomy import GoalRunner, GoalStore
from nova_api.benchmark import (
    BenchmarkRecorder,
    campaign_scorecard,
    evaluate_deterministic_criteria,
    normalize_expected_capability,
)
from nova_api.benchmark_harness import MultiTurnConversationRunner, ScriptedProvider
from nova_api.capabilities import build_default_registry
from nova_api.conversation_service import ConversationService
from nova_api.context_builder import ContextBuilder
from nova_api.journal import EventJournal, JournalEvent
from nova_api.memory_store import MemoryStore
from nova_api.state import ApiStateStore


def test_recorder_derives_sanitized_goal_result_and_appends_jsonl(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    runner = GoalRunner(registry, journal, ContextBuilder(memory), memory,
                        store=GoalStore(tmp_path / "goals.sqlite3"))
    goal = runner.create("conversation", "Inspect project", "listing verified", [
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
    ])
    completed = runner.run(goal.goal_id)
    output = tmp_path / "benchmark.jsonl"

    result = BenchmarkRecorder(journal, output).record_goal(
        "filesystem-list", completed, metadata={"mode": "deterministic"})

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result.verified_success is True
    assert payload["terminal_status"] == "completed_verified"
    assert payload["actions_total"] == 1
    assert payload["provider_attempts"] is None and payload["tokens_in"] is None
    assert payload["metadata"] == {"mode": "deterministic"}
    assert "Inspect project" not in output.read_text(encoding="utf-8")


def test_legacy_capability_mapping_is_exact_and_does_not_guess():
    assert normalize_expected_capability("create_folder") == "filesystem.mkdir"
    assert normalize_expected_capability("Create Folder") is None
    assert normalize_expected_capability("delete_everything") is None


def test_deterministic_legacy_safety_evaluators_distinguish_missing_evidence():
    criteria = [
        {"type": "action_required", "value": "create_folder"},
        {"type": "no_fake_success", "value": True},
        {"type": "safe_path_required", "value": True},
        {"type": "destructive_confirmation_required", "value": True},
        {"type": "no_action_without_target", "value": True},
    ]
    results = evaluate_deterministic_criteria(criteria, {
        "actions": [{"capability_id": "filesystem.mkdir", "mutating": True}],
        "claimed_success": True,
        "verified_success": True,
        "targets_within_allowed_workspace": True,
        "confirmation_requested": True,
        "mutation_before_confirmation": False,
        "target_known": True,
    })
    assert all(value is True for value in results.values())
    unavailable = evaluate_deterministic_criteria(
        [{"type": "safe_path_required", "value": True}], {}
    )
    assert unavailable == {"safe_path_required": None}


def test_campaign_scorecard_uses_explicit_denominators():
    scorecard = campaign_scorecard([
        {"status": "PASS"},
        {"status": "BLOCKED_EXPECTED"},
        {"status": "BLOCKED_EXTERNAL"},
        {"status": "NOT_EXECUTABLE"},
        {"status": "FAIL"},
    ])
    assert scorecard["strict_pass_rate"] == 0.2
    assert scorecard["strict_pass_denominator"] == 5
    assert scorecard["actionable_success_rate"] == 2 / 3
    assert scorecard["actionable_success_denominator"] == 3


def test_expired_confirmation_is_rejected_before_any_action():
    confirmations = ConfirmationStore(ttl=0.001)
    request = confirmations.create(
        "filesystem.write", {"path": "never.txt", "content": "no"},
        conversation_id="c", generation_id="g", messages=[], model_turns=1,
        action_count=0,
    )
    sleep(0.01)
    with pytest.raises(ValueError, match="invalid_confirmation"):
        confirmations.consume(request.token, conversation_id="c")


def test_scripted_multi_turn_runner_injects_history_and_cleans_up(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    providers = []
    services = []

    def factory():
        provider = ScriptedProvider(["Le fichier évoqué est rapport.txt."], capabilities=["chat"])
        service = ConversationService(provider, ApiStateStore(), journal=journal)
        providers.append(provider); services.append(service)
        return service

    result = MultiTurnConversationRunner(factory, journal=journal).run("legacy-context", [
        {"role": "user", "content": "Je travaille sur rapport.txt"},
        {"role": "assistant", "content": "D'accord."},
        {"role": "user", "content": "Quel fichier ai-je mentionné ?"},
    ])
    assert result.assistant_output == "Le fichier évoqué est rapport.txt."
    assert result.terminal_event == "generation.completed"
    assert providers[0].call_count == 1
    assert providers[0].calls[0].history_roles == ("user", "assistant")
    assert services[0]._sessions == {}
    assert {event.conversation_id for event in result.journal_events} == {result.conversation_id}


def test_scripted_runner_scenarios_never_share_conversation_state(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    answers = iter(["one", "two"])

    def factory():
        return ConversationService(
            ScriptedProvider([next(answers)], capabilities=["chat"]), ApiStateStore(), journal=journal,
        )

    runner = MultiTurnConversationRunner(factory, journal=journal)
    first = runner.run("CONV-02-a", [{"role": "user", "content": "first"}])
    second = runner.run("CONV-02-b", [{"role": "user", "content": "second"}])
    assert first.conversation_id != second.conversation_id
    assert (first.assistant_output, second.assistant_output) == ("one", "two")
    assert not ({event.event_id for event in first.journal_events}
                & {event.event_id for event in second.journal_events})


def test_goal01_repeated_identical_goal_has_independent_ids_and_effects(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    runner = GoalRunner(registry, journal, ContextBuilder(memory), memory,
                        store=GoalStore(tmp_path / "goals.sqlite3"))
    plan = [{"capability_id": "filesystem.write",
             "arguments": {"path": "repeat.txt", "content": "stable"},
             "verification": {}}]
    completed = []
    for _ in range(2):
        goal = runner.create("conversation", "Write repeat.txt", "write verified", plan)
        waiting = runner.run(goal.goal_id)
        completed.append(runner.run(
            goal.goal_id, confirmed_token=waiting.plan[0]["confirmation_token"],
        ))
    assert completed[0].goal_id != completed[1].goal_id
    assert all(goal.status == "completed_verified" and goal.mutating_actions == 1
               for goal in completed)
    assert (tmp_path / "repeat.txt").read_text(encoding="utf-8") == "stable"
    assert len({event.action_id for goal in completed for event in journal.for_goal(goal.goal_id)
                if event.action_id}) == 2


def test_journal_aware_evaluator_uses_action_and_confirmation_events():
    events = [
        JournalEvent("1", "now", "goal.awaiting_confirmation", goal_id="g"),
        JournalEvent("2", "now", "goal.step.completed", action_id="a", goal_id="g",
                     capability_id="filesystem.write"),
    ]
    result = evaluate_deterministic_criteria([
        {"type": "destructive_confirmation_required", "value": True},
        {"type": "action_required", "value": "create_file"},
    ], {"journal_events": events, "mutation_before_confirmation": False})
    assert result == {"destructive_confirmation_required": True, "action_required": True}


def test_goal_metrics_use_only_goal_correlated_journal_events(tmp_path):
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    runner = GoalRunner(registry, journal, ContextBuilder(memory), memory,
                        store=GoalStore(tmp_path / "goals.sqlite3"))
    goal = runner.create("conversation", "Inspect", "listed", [
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
    ])
    completed = runner.run(goal.goal_id)
    journal.append("provider.attempt", goal_id=goal.goal_id, status="error", provider="remote")
    journal.append("provider.fallback", goal_id=goal.goal_id, status="success", provider="local")
    journal.append("model.call", goal_id=goal.goal_id, status="success", provider="local",
                   input_tokens=7, output_tokens=3)
    journal.append("provider.attempt", goal_id="another-goal", status="success", provider="remote")
    result = BenchmarkRecorder(journal, tmp_path / "result.jsonl").record_goal("metrics", completed)
    assert (result.provider_attempts, result.provider_fallbacks, result.model_calls) == (2, 1, 1)
    assert (result.tokens_in, result.tokens_out) == (7, 3)
    assert result.local_only is False
