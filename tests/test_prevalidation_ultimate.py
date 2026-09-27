from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import model_router
from nova_api.autonomy import GoalRunner, GoalStore
from nova_api.computer import ComputerController, ComputerError, NativeUIElement, NativeWindow
from nova_api.journal import EventJournal
from nova_api.memory_store import MemoryStore
from nova_api.context_builder import ContextBuilder
from nova_api.transactions import TransactionError


class Desktop:
    def __init__(self):
        self.item = NativeWindow(10, 1, "app.exe", "App", "normal")
    def windows(self): return [self.item]
    def active_window_id(self): return 10
    def displays(self): return []
    def focus(self, _native_id): return True
    def minimize(self, _native_id): return True
    def restore(self, _native_id): return True


class ChangingUI:
    def __init__(self):
        self.calls = 0
        self.invoked = False
    def elements(self, _native_window_id, *, depth, max_elements):
        del depth, max_elements
        self.calls += 1
        # inspect_ui is call 1; first action resolution is call 2; the mandatory
        # pre-mutation re-observation is call 3 and sees a different target.
        name = "Delete account" if self.calls >= 3 else "Save"
        return [NativeUIElement("button", "button", name, automation_id="", path=(),
                                bounds=(10, 10, 100, 40), actions=("invoke",))]
    def invoke(self, _native_key): self.invoked = True; return True
    def focus(self, _native_key): return True
    def set_value(self, _native_key, _value): return True
    def toggle(self, _native_key): return True
    def select(self, _native_key): return True


def test_ui_mutation_reobserves_and_rejects_changed_target():
    ui = ChangingUI()
    computer = ComputerController(Desktop(), ui)
    window_ref = computer.windows()["windows"][0]["window_ref"]
    element_ref = computer.inspect_ui(window_ref)["elements"][0]["element_ref"]
    with pytest.raises(ComputerError, match="TARGET_FINGERPRINT_CHANGED"):
        computer.ui_action("invoke", element_ref)
    assert ui.invoked is False


def test_provider_health_uses_global_bounded_exponential_backoff(monkeypatch):
    clock = {"value": 1000.0}
    monkeypatch.setattr(model_router.time, "monotonic", lambda: clock["value"])
    model_router.reset_provider_health()
    model_router._mark_provider_error("groq", "network_error")
    first = model_router._get_provider_state("groq").cooldown_until - clock["value"]
    clock["value"] += first + 1
    assert model_router._is_provider_available("groq") is True
    model_router._mark_provider_error("groq", "network_error")
    second = model_router._get_provider_state("groq").cooldown_until - clock["value"]
    assert first == pytest.approx(30.0)
    assert second == pytest.approx(60.0)
    model_router._mark_provider_success("groq")
    assert model_router._get_provider_state("groq").consecutive_failures == 0


def test_goal_store_fails_closed_on_newer_schema(tmp_path):
    path = tmp_path / "goals.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="unsupported_goal_store_schema_version:99"):
        GoalStore(path)


def test_rollback_failure_is_visible_and_not_silently_ignored(tmp_path):
    class Transactions:
        def rollback(self, _transaction_id):
            raise TransactionError("transaction_conflict")
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = SimpleNamespace(transactions=Transactions())
    runner = object.__new__(GoalRunner)
    runner.registry = registry
    runner.journal = journal
    attempted, error = runner._rollback_if_safer(True, {"transaction_id": "tx-1"})
    assert attempted is True
    assert error == "transaction_conflict"
    event = journal.recent(limit=1)[0]
    assert event.type == "goal.rollback.failed"
    assert event.error_category == "transaction_conflict"


def test_target_fingerprint_change_recovers_by_reobserving():
    from nova_api.recovery import RecoveryPolicy
    decision = RecoveryPolicy().decide(failure_category="TARGET_FINGERPRINT_CHANGED", uncertainty_level="high")
    assert decision.action == "reobserve"


def test_running_goal_cancel_is_request_until_runner_observes_it(tmp_path):
    from dataclasses import replace
    from nova_api.capabilities import build_default_registry

    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    runner = GoalRunner(registry, journal, ContextBuilder(memory), memory,
                        store=GoalStore(tmp_path / "goals.sqlite3"))
    goal = runner.create("c", "Inspect project", "Project is observed", [
        {"capability_id": "project.basic_info", "arguments": {}}
    ])
    runner.store.save(replace(goal, status="running", phase="execute"))

    requested = runner.cancel(goal.goal_id)
    assert requested.status == "running"
    assert journal.recent(limit=1)[0].type == "goal.cancel_requested"

    final = runner.run(goal.goal_id)
    assert final.status == "cancelled"
    assert "cancel_requested" in final.blockers
