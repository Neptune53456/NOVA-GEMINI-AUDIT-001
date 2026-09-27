from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import contextmanager
import json
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from nova_api.agent_loop import AgentLoop, EXPOSED_CAPABILITIES, select_capabilities, tool_definitions
from nova_api.engine_adapter import NovaEngineAdapter
from nova_api.app import create_app
from nova_api.capabilities import ANTI_SPIN_THRESHOLD, build_default_registry
from nova_api.computer import (ComputerController, ComputerError, NativeUIElement, NativeWindow,
                               WindowsDesktopBackend)
from nova_api.journal import EventJournal
from nova_api.missions import MissionManager, MissionStore
from nova_api.windows_uia import ComtypesUIAutomationBackend


class FakeDesktop:
    def __init__(self) -> None:
        self.items = [NativeWindow(101, 10, "Code.exe", "Private project - Code", "normal"),
                      NativeWindow(202, 20, "notepad.exe", "Notes", "minimized")]
        self.active = 101
        self.verify_actions = True

    def windows(self): return list(self.items)
    def active_window_id(self): return self.active
    def displays(self): return [{"index": 0, "width": 1920, "height": 1080, "primary": True}]
    def focus(self, native_id):
        if self.verify_actions: self.active = native_id
        return True
    def minimize(self, native_id):
        if self.verify_actions: self._state(native_id, "minimized")
        return True
    def restore(self, native_id):
        if self.verify_actions: self._state(native_id, "normal")
        return True
    def _state(self, native_id, state):
        self.items = [NativeWindow(item.native_id, item.process_id, item.application, item.title,
                                   state if item.native_id == native_id else item.state) for item in self.items]


class FakeUIAutomation:
    def __init__(self):
        self.items = [
            NativeUIElement("root", "window", "Notes", actions=("focus",)),
            NativeUIElement("save", "button", "Enregistrer", "root", actions=("invoke",)),
            NativeUIElement("editor", "document", "Éditeur", "root", editable=True,
                            read_only=False, value="secret existing text", actions=("focus", "set_value")),
            NativeUIElement("password", "edit", "Mot de passe", "root", editable=True,
                            read_only=False, password=True, value="never expose", actions=("set_value",)),
        ]
        self.invoked = False
        self.mismatch = False

    def elements(self, native_window_id, *, depth, max_elements):
        del native_window_id, depth
        return list(self.items[:max_elements])
    def invoke(self, native_key): self.invoked = native_key == "save"; return self.invoked
    def focus(self, native_key): return self._replace(native_key, focused=True)
    def set_value(self, native_key, value): return self._replace(native_key, value="mismatch" if self.mismatch else value)
    def toggle(self, native_key):
        item = self._find(native_key); return self._replace(native_key, checked=not item.checked)
    def select(self, native_key): return self._replace(native_key, selected=True)
    def _find(self, key): return next(item for item in self.items if item.native_key == key)
    def _replace(self, key, **changes):
        from dataclasses import replace
        self.items = [replace(item, **changes) if item.native_key == key else item for item in self.items]
        return True


@pytest.fixture
def computer_stack(tmp_path):
    backend = FakeDesktop()
    ui = FakeUIAutomation()
    backend.ui = ui
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path,
                                      computer=ComputerController(backend, ui))
    return backend, journal, registry


def test_observation_is_sanitized_and_uses_opaque_stable_references(computer_stack):
    _backend, _journal, registry = computer_stack
    first = registry.execute("computer.observe")
    second = registry.execute("computer.windows")
    assert first.status == "success" and first.verified
    state = first.result
    assert state["active_window"]["application"] == "Code.exe"
    assert state["windows"][0]["window_ref"].startswith("window_")
    assert state["windows"][0]["window_ref"] == second.result["windows"][0]["window_ref"]
    serialized = repr(state).casefold()
    assert "native_id" not in serialized and "process_id" not in serialized and "environment" not in serialized


def test_stale_reference_and_missing_active_window_are_structured(computer_stack):
    backend, _journal, registry = computer_stack
    ref = registry.execute("computer.windows").result["windows"][0]["window_ref"]
    backend.items = []
    backend.active = None
    stale = registry.execute("computer.window.focus", {"window_ref": ref})
    active = registry.execute("computer.active_window")
    assert stale.error_category == "STALE_WINDOW_REF"
    assert active.error_category == "ACTIVE_WINDOW_UNAVAILABLE"


@pytest.mark.parametrize(("capability", "expected_state"), [
    ("computer.window.minimize", "minimized"),
    ("computer.window.restore", "normal"),
])
def test_window_actions_are_verified(computer_stack, capability, expected_state):
    _backend, _journal, registry = computer_stack
    windows = registry.execute("computer.windows").result["windows"]
    target = windows[0] if capability.endswith("minimize") else windows[1]
    result = registry.execute(capability, {"window_ref": target["window_ref"]})
    assert result.status == "success"
    assert result.result["verification_status"] == "verified"
    assert result.result["observation_after"]["window"]["state"] == expected_state


def test_focus_failure_is_not_assumed_and_anti_spin_bounds_retries(computer_stack):
    backend, journal, registry = computer_stack
    ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    backend.verify_actions = False
    results = [registry.execute("computer.window.focus", {"window_ref": ref})
               for _ in range(ANTI_SPIN_THRESHOLD + 1)]
    assert results[0].result["execution_status"] == "completed"
    assert results[0].error_category == "VERIFICATION_FAILED"
    assert results[-1].error_category == "repeated_action"
    assert any(event.type == "computer.action.verification_failed" for event in journal.recent())


def test_observation_needs_no_confirmation_and_journal_has_only_safe_metadata(computer_stack):
    _backend, journal, registry = computer_stack
    capability = registry.lookup("computer.observe")
    assert capability.requires_confirmation is False
    registry.execute("computer.observe")
    event = journal.recent(limit=1)[0]
    assert event.type == "computer.observed"
    metadata = asdict(event)
    required = {"event_id", "timestamp", "type", "generation_id", "action_id",
                "transaction_id", "conversation_id", "mission_id", "goal_id", "capability_id", "status",
                "duration_ms", "model", "provider", "input_tokens", "output_tokens", "error_category",
                "structural_fingerprint"}
    assert required <= metadata.keys()
    assert {"arguments", "content", "result", "window_title", "ui_text"}.isdisjoint(metadata)
    assert metadata["type"] == "computer.observed" and metadata["status"] == "success"


def test_non_windows_backend_fails_without_import_failure(monkeypatch):
    monkeypatch.setattr("nova_api.computer.os.name", "posix")
    with pytest.raises(ComputerError, match="PLATFORM_UNSUPPORTED"):
        WindowsDesktopBackend()


def test_com_initialization_failure_is_sanitized(monkeypatch, computer_stack):
    _backend, journal, _registry = computer_stack
    native = ComtypesUIAutomationBackend()

    def fail_initialization():
        raise Exception("native COM details and HRESULT must not leak")

    monkeypatch.setattr("comtypes.CoInitialize", fail_initialization)
    controller = ComputerController(FakeDesktop(), native)
    registry = build_default_registry(journal, computer=controller)
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    result = registry.execute("computer.ui.elements", {"window_ref": window_ref})

    assert result.status == "error"
    assert result.error_category == "UI_AUTOMATION_INITIALIZATION_FAILED"
    assert "native COM details" not in repr(result)
    event = journal.recent(limit=1)[0]
    assert event.type == "computer.action.failed"
    assert event.error_category == "UI_AUTOMATION_INITIALIZATION_FAILED"


class InspectFailureThenStopModel:
    def __init__(self): self.turn = 0
    def agent_turn(self, messages, tools, *, timeout_seconds):
        del tools, timeout_seconds
        self.turn += 1
        if self.turn == 1:
            return {"role": "assistant", "tool_calls": [{"function": {
                "name": "computer.windows", "arguments": {}}}]}
        if self.turn == 2:
            window_ref = json.loads(messages[-1]["content"])["windows"][1]["window_ref"]
            return {"role": "assistant", "tool_calls": [{"function": {
                "name": "computer.ui.elements", "arguments": {"window_ref": window_ref}}}]}
        observation = json.loads(messages[-1]["content"])
        assert observation["status"] == "error"
        assert observation["error_category"] == "UI_AUTOMATION_QUERY_FAILED"
        assert "native provider secret" not in messages[-1]["content"]
        return {"role": "assistant", "content": "Inspection indisponible."}


def test_native_uia_exception_does_not_escape_agent_loop(computer_stack):
    backend, journal, registry = computer_stack

    def fail_query(*_args, **_kwargs):
        raise Exception("native provider secret")

    backend.ui.elements = fail_query
    outcome = AgentLoop(InspectFailureThenStopModel(), registry, journal).run(
        "Quels éléments interactifs vois-tu dans cette fenêtre ?", [], "agent", Event(),
        conversation_id="c", generation_id="g", notify=lambda *_: None)

    assert outcome.status == "success"
    events = journal.recent()
    assert any(event.type == "computer.action.failed" and
               event.error_category == "UI_AUTOMATION_QUERY_FAILED" for event in events)
    assert any(event.type == "agent.tool_completed" and event.status == "error" for event in events)
    assert "native provider secret" not in repr(events)


def test_inaccessible_individual_uia_element_is_skipped(monkeypatch):
    native = ComtypesUIAutomationBackend()

    class Element:
        CurrentControlType = 50032
        CurrentName = "root"
        CurrentIsKeyboardFocusable = False
        CurrentIsPassword = False
        CurrentIsEnabled = True
        CurrentIsOffscreen = False
        CurrentHasKeyboardFocus = False
        def __init__(self, runtime_id=None): self.runtime_id = runtime_id
        def GetRuntimeId(self):
            if self.runtime_id is None: raise Exception("inaccessible child")
            return self.runtime_id
        def GetCurrentPattern(self, _pattern): return None

    root, inaccessible = Element([1]), Element()

    class Walker:
        def GetFirstChildElement(self, element): return inaccessible if element is root else None
        def GetNextSiblingElement(self, _element): return None

    automation = SimpleNamespace(ElementFromHandle=lambda _handle: root, ControlViewWalker=Walker())
    uia = SimpleNamespace(UIA_ValuePatternId=1, IUIAutomationValuePattern=object,
        UIA_TogglePatternId=2, IUIAutomationTogglePattern=object,
        UIA_SelectionItemPatternId=3, IUIAutomationSelectionItemPattern=object,
        UIA_InvokePatternId=4, IUIAutomationInvokePattern=object)

    @contextmanager
    def session(): yield uia, automation

    monkeypatch.setattr(native, "_session", session)
    result = native.elements(202, depth=2, max_elements=10)
    assert [item.native_key for item in result] == [(202, (1,))]


class ObserveThenFocusModel:
    def __init__(self): self.turn = 0
    def agent_turn(self, messages, tools, *, timeout_seconds):
        self.turn += 1
        if self.turn == 1:
            return {"role": "assistant", "tool_calls": [{"function": {"name": "computer.windows", "arguments": {}}}]}
        return {"role": "assistant", "content": "Bureau observé."}


class ObserveInspectActModel:
    def __init__(self): self.turn = 0
    def agent_turn(self, messages, tools, *, timeout_seconds):
        del timeout_seconds
        self.turn += 1
        assert "computer.ui.set_value" in {item["function"]["name"] for item in tools}
        if self.turn == 1:
            return {"role": "assistant", "tool_calls": [{"function": {"name": "computer.windows", "arguments": {}}}]}
        previous = json.loads(messages[-1]["content"])
        if self.turn == 2:
            return {"role": "assistant", "tool_calls": [{"function": {"name": "computer.ui.inspect",
                "arguments": {"window_ref": previous["windows"][1]["window_ref"]}}}]}
        if self.turn == 3:
            editor = next(item for item in previous["elements"] if item.get("editable") and not item.get("protected"))
            return {"role": "assistant", "tool_calls": [{"function": {"name": "computer.ui.set_value",
                "arguments": {"element_ref": editor["element_ref"], "value": "Bonjour Lucas"}}}]}
        return {"role": "assistant", "content": "Texte saisi et vérifié."}


class NormalNotepadSetValueModel:
    def __init__(self): self.turn = 0
    def agent_turn(self, messages, tools, *, timeout_seconds):
        del tools, timeout_seconds
        self.turn += 1
        if self.turn == 1:
            guidance = messages[0]["content"]
            assert "ne restaure qu'une fenêtre minimized" in guidance
            assert "set_value ne requiert pas que la fenêtre soit au premier plan" in guidance
            return {"role": "assistant", "tool_calls": [{"function": {
                "name": "computer.windows", "arguments": {}}}]}
        previous = json.loads(messages[-1]["content"])
        if self.turn == 2:
            target = next(item for item in previous["windows"] if item["application"] == "notepad.exe")
            assert target["state"] == "normal"
            return {"role": "assistant", "tool_calls": [{"function": {
                "name": "computer.ui.inspect", "arguments": {"window_ref": target["window_ref"]}}}]}
        if self.turn == 3:
            editor = next(item for item in previous["elements"]
                          if item.get("editable") and "set_value" in item["supported_actions"])
            return {"role": "assistant", "tool_calls": [{"function": {
                "name": "computer.ui.set_value",
                "arguments": {"element_ref": editor["element_ref"], "value": "Bonjour Lucas"}}}]}
        assert previous["status"] == "success" and previous["verified"] is True
        return {"role": "assistant", "content": "Texte saisi et vérifié."}


def test_agent_loop_discovers_and_invokes_computer_capability(computer_stack):
    _backend, journal, registry = computer_stack
    assert {item["function"]["name"] for item in tool_definitions(registry)} == EXPOSED_CAPABILITIES
    outcome = AgentLoop(ObserveThenFocusModel(), registry, journal).run(
        "Observe le bureau", [], "agent", Event(), conversation_id="c", generation_id="g", notify=lambda *_: None)
    assert outcome.status == "success"
    assert any(event.capability_id == "computer.windows" for event in journal.recent())


def test_agent_loop_observes_inspects_and_acts_semantically(computer_stack):
    _backend, journal, registry = computer_stack
    outcome = AgentLoop(ObserveInspectActModel(), registry, journal, max_model_turns=4).run(
        "Écris Bonjour Lucas dans la fenêtre", [], "agent", Event(), conversation_id="c",
        generation_id="g", notify=lambda *_: None)
    assert outcome.status == "success"
    assert any(event.capability_id == "computer.ui.set_value" for event in journal.recent())


def test_normal_notepad_converges_to_set_value_without_focus_or_restore(computer_stack):
    backend, journal, registry = computer_stack
    backend._state(202, "normal")
    outcome = AgentLoop(NormalNotepadSetValueModel(), registry, journal).run(
        "Écris Bonjour Lucas dans le Bloc-notes.", [], "agent", Event(), conversation_id="c",
        generation_id="g", notify=lambda *_: None)

    assert outcome.status == "success"
    requested = [event.capability_id for event in journal.recent()
                 if event.type == "agent.tool_requested"]
    assert list(reversed(requested)) == [
        "computer.windows", "computer.ui.inspect", "computer.ui.set_value",
    ]
    assert backend.ui._find("editor").value == "Bonjour Lucas"
    assert outcome.completion_state == "completed_verified"


def test_verified_uia_mutation_skips_final_provider_timeout(computer_stack):
    backend, journal, registry = computer_stack
    backend._state(202, "normal")

    class Model(NormalNotepadSetValueModel):
        def agent_turn(self, messages, tools, *, timeout_seconds):
            if self.turn == 3:
                raise TimeoutError("final narration must be skipped")
            return super().agent_turn(messages, tools, timeout_seconds=timeout_seconds)

    model = Model()
    outcome = AgentLoop(model, registry, journal).run(
        "Écris Bonjour Lucas dans le Bloc-notes.", [], "agent", Event(),
        conversation_id="c", generation_id="g", notify=lambda *_: None)

    assert outcome.status == "success"
    assert outcome.completion_state == "completed_verified"
    assert model.turn == 3
    assert backend.ui._find("editor").value == "Bonjour Lucas"


def test_reacquired_window_still_completes_verified_without_extra_model_call(computer_stack):
    backend, journal, registry = computer_stack
    backend._state(202, "normal")

    class Model(NormalNotepadSetValueModel):
        def agent_turn(self, messages, tools, *, timeout_seconds):
            if self.turn == 2:
                backend.items[1] = replace(backend.items[1], native_id=303, process_id=30)
                backend.ui.items = [replace(item, native_key="editor-reopened")
                                    if item.native_key == "editor" else item for item in backend.ui.items]
            return super().agent_turn(messages, tools, timeout_seconds=timeout_seconds)

    model = Model()
    outcome = AgentLoop(model, registry, journal).run(
        "Écris Bonjour Lucas dans le Bloc-notes.", [], "agent", Event(),
        conversation_id="c", generation_id="g", notify=lambda *_: None)

    assert outcome.status == "success" and outcome.completion_state == "completed_verified"
    assert model.turn == 3
    assert backend.ui._find("editor-reopened").value == "Bonjour Lucas"


def test_minimized_window_can_restore_then_reach_semantic_action(computer_stack):
    backend, _journal, registry = computer_stack

    class Model:
        turn = 0
        window_ref = None
        def agent_turn(self, messages, tools, *, timeout_seconds):
            del tools, timeout_seconds
            self.turn += 1
            previous = json.loads(messages[-1]["content"]) if self.turn > 1 else None
            if self.turn == 1:
                return {"role": "assistant", "tool_calls": [{"function": {
                    "name": "computer.windows", "arguments": {}}}]}
            if self.turn == 2:
                target = next(item for item in previous["windows"] if item["application"] == "notepad.exe")
                assert target["state"] == "minimized"
                self.window_ref = target["window_ref"]
                return {"role": "assistant", "tool_calls": [{"function": {
                    "name": "computer.window.restore", "arguments": {"window_ref": self.window_ref}}}]}
            if self.turn == 3:
                assert previous["status"] == "success"
                return {"role": "assistant", "tool_calls": [{"function": {
                    "name": "computer.ui.inspect", "arguments": {"window_ref": self.window_ref}}}]}
            if self.turn == 4:
                editor = next(item for item in previous["elements"] if item.get("editable"))
                return {"role": "assistant", "tool_calls": [{"function": {
                    "name": "computer.ui.set_value",
                    "arguments": {"element_ref": editor["element_ref"], "value": "Bonjour Lucas"}}}]}
            return {"role": "assistant", "content": "Récupération terminée."}

    outcome = AgentLoop(Model(), registry, _journal).run(
        "Écris Bonjour Lucas dans la fenêtre minimisée.", [], "agent", Event(),
        conversation_id="c", generation_id="g", notify=lambda *_: None)
    assert outcome.status == "success"
    assert backend.items[1].state == "normal"
    assert backend.ui._find("editor").value == "Bonjour Lucas"


def test_repeated_discovery_is_rejected_locally_but_model_can_recover(computer_stack):
    _backend, journal, registry = computer_stack

    class Model:
        turn = 0
        def agent_turn(self, messages, tools, *, timeout_seconds):
            del tools, timeout_seconds
            self.turn += 1
            if self.turn <= 2:
                return {"role": "assistant", "tool_calls": [{"function": {
                    "name": "computer.windows", "arguments": {}}}]}
            assert json.loads(messages[-1]["content"])["error_category"] == "repeated_observation"
            return {"role": "assistant", "content": "Observation déjà disponible."}

    model = Model()
    outcome = AgentLoop(model, registry, journal).run(
        "Observe les fenêtres", [], "agent", Event(), conversation_id="c", generation_id="g",
        notify=lambda *_: None)
    assert outcome.status == "error" and outcome.error_category == "repeated_observation"
    assert model.turn == 2
    assert sum(event.type == "computer.observed" for event in journal.recent()) == 2


def test_recreated_window_is_new_observation_evidence(computer_stack):
    backend, journal, registry = computer_stack

    class Model:
        turn = 0
        def agent_turn(self, messages, tools, *, timeout_seconds):
            del messages, tools, timeout_seconds
            self.turn += 1
            if self.turn == 2:
                backend.items[1] = replace(backend.items[1], native_id=303, process_id=30)
            if self.turn <= 2:
                return {"role": "assistant", "tool_calls": [{"function": {
                    "name": "computer.windows", "arguments": {}}}]}
            return {"role": "assistant", "content": "Nouvelle instance observée."}

    model = Model()
    outcome = AgentLoop(model, registry, journal).run(
        "Observe la fenêtre pendant sa réouverture", [], "agent", Event(), conversation_id="c", generation_id="g",
        notify=lambda *_: None)

    assert outcome.status == "success" and model.turn == 3
    assert not any(event.error_category == "repeated_observation" for event in journal.recent())


def test_failed_mutation_consumes_finite_action_budget(computer_stack):
    backend, journal, registry = computer_stack
    backend.verify_actions = False

    class Model:
        turn = 0
        ref = None
        def agent_turn(self, messages, tools, *, timeout_seconds):
            del tools, timeout_seconds
            self.turn += 1
            if self.turn == 1:
                return {"role": "assistant", "tool_calls": [{"function": {
                    "name": "computer.windows", "arguments": {}}}]}
            if self.ref is None:
                self.ref = json.loads(messages[-1]["content"])["windows"][1]["window_ref"]
            return {"role": "assistant", "tool_calls": [{"function": {
                "name": "computer.window.focus", "arguments": {"window_ref": self.ref}}}]}

    outcome = AgentLoop(Model(), registry, journal, max_actions=1).run(
        "Focus cette fenêtre", [], "agent", Event(), conversation_id="c", generation_id="g",
        notify=lambda *_: None)
    assert outcome.status == "error" and outcome.error_category == "action_budget_exceeded"
    completed = [event for event in journal.recent() if event.type == "agent.tool_completed"]
    assert sum(event.capability_id == "computer.window.focus" for event in completed) == 1


def test_mission_routes_computer_action_through_registry(computer_stack, tmp_path):
    _backend, journal, registry = computer_stack
    ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    manager = MissionManager(registry, journal, store=MissionStore(tmp_path / "missions.sqlite3"))
    mission = manager.create("conversation", "Focus notes", [
        {"capability_id": "computer.window.focus", "arguments": {"window_ref": ref}}])
    completed = manager.run(mission.mission_id)
    assert completed.state == "completed"
    assert completed.checkpoint["last_result"]["verification_status"] == "verified"


def test_mission_executes_verified_semantic_ui_step(computer_stack, tmp_path):
    _backend, journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    elements = registry.execute("computer.ui.inspect", {"window_ref": window_ref}).result["elements"]
    editor = next(item for item in elements if item.get("editable") and not item.get("protected"))
    manager = MissionManager(registry, journal, store=MissionStore(tmp_path / "ui-missions.sqlite3"))
    mission = manager.create("conversation", "Write notes", [{"capability_id": "computer.ui.set_value",
        "arguments": {"element_ref": editor["element_ref"], "value": "Mission text"}}])
    assert manager.run(mission.mission_id).state == "completed"


def test_read_only_computer_state_api(computer_stack):
    _backend, journal, registry = computer_stack
    client = TestClient(create_app(journal=journal, capability_registry=registry))
    response = client.get("/api/v1/computer/state")
    assert response.status_code == 200
    assert response.json()["window_count"] == 2


def test_ui_observation_is_bounded_opaque_and_protects_passwords(computer_stack):
    _backend, _journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    result = registry.execute("computer.ui.inspect", {"window_ref": window_ref, "max_elements": 3, "depth": 2})
    assert result.status == "success"
    assert result.result["element_count"] == 3 and result.result["truncated"] is True
    assert all(item["element_ref"].startswith("element_") for item in result.result["elements"])
    serialized = repr(result.result).casefold()
    assert "native_key" not in serialized and "process_id" not in serialized and "native_id" not in serialized
    protected = registry.execute("computer.ui.inspect", {"window_ref": window_ref}).result["elements"][-1]
    assert protected["protected"] is True and "value" not in protected and "never expose" not in repr(protected)


def test_ui_actions_verify_set_value_and_keep_invoke_unverifiable(computer_stack):
    backend, journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    elements = registry.execute("computer.ui.inspect", {"window_ref": window_ref}).result["elements"]
    save = next(item for item in elements if item["role"] == "button")
    editor = next(item for item in elements if item.get("editable") and not item.get("protected"))
    invoked = registry.execute("computer.ui.invoke", {"element_ref": save["element_ref"]})
    changed = registry.execute("computer.ui.set_value", {"element_ref": editor["element_ref"], "value": "Bonjour Lucas"})
    assert backend.ui.invoked is True
    assert invoked.status == "success" and invoked.verified is False
    assert invoked.result["verification_status"] == "unverifiable"
    assert changed.status == "success" and changed.verified is True
    assert "Bonjour Lucas" not in repr(journal.recent())


def test_stale_wrong_window_unsupported_and_mismatch_fail_structurally(computer_stack):
    backend, _journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    elements = registry.execute("computer.ui.inspect", {"window_ref": window_ref}).result["elements"]
    save = next(item for item in elements if item["role"] == "button")
    editor = next(item for item in elements if item.get("editable") and not item.get("protected"))
    unsupported = registry.execute("computer.ui.set_value", {"element_ref": save["element_ref"], "value": "x"})
    assert unsupported.error_category == "ELEMENT_ACTION_UNSUPPORTED"
    backend.ui.mismatch = True
    mismatch = registry.execute("computer.ui.set_value", {"element_ref": editor["element_ref"], "value": "expected"})
    assert mismatch.error_category == "VERIFICATION_FAILED"
    backend.ui.items = [item for item in backend.ui.items if item.native_key != "editor"]
    stale = registry.execute("computer.ui.focus", {"element_ref": editor["element_ref"]})
    assert stale.error_category == "STALE_ELEMENT_REFERENCE"


def test_element_ref_invalidates_when_its_window_disappears(computer_stack):
    backend, _journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    element_ref = registry.execute("computer.ui.inspect", {"window_ref": window_ref}).result["elements"][0]["element_ref"]
    backend.items = backend.items[:1]
    result = registry.execute("computer.ui.focus", {"element_ref": element_ref})
    assert result.error_category == "STALE_ELEMENT_REFERENCE"


def test_destroyed_top_level_window_is_reacquired_before_mutation(computer_stack):
    backend, _journal, registry = computer_stack
    old_window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    editor = next(item for item in registry.execute("computer.ui.inspect", {
        "window_ref": old_window_ref}).result["elements"] if item.get("editable") and not item.get("protected"))
    backend.items[1] = replace(backend.items[1], native_id=303, process_id=30)
    backend.ui.items = [replace(item, native_key="editor-new", bounds=(500, 400, 900, 700))
                        if item.native_key == "editor" else item for item in backend.ui.items]

    context = registry.risk_context("computer.ui.set_value", {"element_ref": editor["element_ref"]})

    result = registry.execute("computer.ui.set_value", {
        "element_ref": editor["element_ref"], "value": "NOVA TEST WINDOWS REOPEN"})

    assert result.status == "success" and result.verified is True
    assert context["application"] == "notepad.exe" and context.get("ambiguous") is not True
    assert result.result["target"]["window_ref"] != old_window_ref
    assert backend.ui._find("editor-new").value == "NOVA TEST WINDOWS REOPEN"


def test_same_process_recreated_window_is_reacquired(computer_stack):
    backend, _journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    editor = next(item for item in registry.execute("computer.ui.inspect", {
        "window_ref": window_ref}).result["elements"] if item.get("editable") and not item.get("protected"))
    backend.items[1] = replace(backend.items[1], native_id=303)
    backend.ui.items = [replace(item, native_key="editor-reopened")
                        if item.native_key == "editor" else item for item in backend.ui.items]

    result = registry.execute("computer.ui.set_value", {
        "element_ref": editor["element_ref"], "value": "same process"})

    assert result.status == "success" and backend.ui._find("editor-reopened").value == "same process"


def test_multiple_recreated_windows_are_ambiguous_and_never_acted_on(computer_stack):
    backend, _journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    editor = next(item for item in registry.execute("computer.ui.inspect", {
        "window_ref": window_ref}).result["elements"] if item.get("editable") and not item.get("protected"))
    original = backend.items[1]
    backend.items = [backend.items[0], replace(original, native_id=303, process_id=30),
                     replace(original, native_id=404, process_id=40)]

    result = registry.execute("computer.ui.set_value", {
        "element_ref": editor["element_ref"], "value": "must not be written"})

    assert result.error_category == "AMBIGUOUS_WINDOW_REPLACEMENT"
    assert backend.ui._find("editor").value == "secret existing text"


def test_old_element_ref_cannot_jump_to_reused_window_handle(computer_stack):
    backend, _journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    element_ref = registry.execute("computer.ui.inspect", {
        "window_ref": window_ref}).result["elements"][0]["element_ref"]
    backend.items[1] = replace(backend.items[1], process_id=999, title="Different window")

    result = registry.execute("computer.ui.focus", {"element_ref": element_ref})

    assert result.error_category == "STALE_ELEMENT_REFERENCE"


def test_element_ref_survives_unrelated_observation_generation_change(computer_stack):
    backend, _journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    editor = next(item for item in registry.execute("computer.ui.inspect", {
        "window_ref": window_ref}).result["elements"] if item.get("editable") and not item.get("protected"))
    backend.items[0] = replace(backend.items[0], bounds=(10, 10, 900, 700))

    result = registry.execute("computer.ui.set_value", {
        "element_ref": editor["element_ref"], "value": "generation recovery"})

    assert result.status == "success" and result.verified is True


def test_stale_native_key_is_uniquely_reresolved_once(computer_stack):
    backend, _journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    backend.ui.items = [replace(item, automation_id="editor-id", path=(0, 1))
                        if item.native_key == "editor" else item for item in backend.ui.items]
    editor = next(item for item in registry.execute("computer.ui.inspect", {
        "window_ref": window_ref}).result["elements"] if item.get("editable") and not item.get("protected"))
    backend.ui.items = [replace(item, native_key="editor-recreated")
                        if item.native_key == "editor" else item for item in backend.ui.items]

    result = registry.execute("computer.ui.set_value", {
        "element_ref": editor["element_ref"], "value": "re-resolved"})

    assert result.status == "success" and backend.ui._find("editor-recreated").value == "re-resolved"


def test_stale_during_action_gets_only_one_bounded_retry(computer_stack):
    backend, _journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    backend.ui.items = [replace(item, automation_id="editor-id", path=(0, 1))
                        if item.native_key == "editor" else item for item in backend.ui.items]
    editor = next(item for item in registry.execute("computer.ui.inspect", {
        "window_ref": window_ref}).result["elements"] if item.get("editable") and not item.get("protected"))
    calls = 0
    original = backend.ui.set_value

    def stale_once(native_key, value):
        nonlocal calls
        calls += 1
        if calls == 1:
            backend.ui.items = [replace(item, native_key="editor-recreated")
                                if item.native_key == native_key else item for item in backend.ui.items]
            raise ComputerError("STALE_ELEMENT_REFERENCE")
        return original(native_key, value)

    backend.ui.set_value = stale_once
    result = registry.execute("computer.ui.set_value", {
        "element_ref": editor["element_ref"], "value": "bounded retry"})

    assert result.status == "success" and calls == 2


def test_stale_native_key_with_multiple_matches_fails_ambiguously(computer_stack):
    backend, _journal, registry = computer_stack
    window_ref = registry.execute("computer.windows").result["windows"][1]["window_ref"]
    backend.ui.items = [replace(item, automation_id="duplicate", path=())
                        if item.native_key == "editor" else item for item in backend.ui.items]
    editor = next(item for item in registry.execute("computer.ui.inspect", {
        "window_ref": window_ref}).result["elements"] if item.get("editable") and not item.get("protected"))
    original = backend.ui._find("editor")
    backend.ui.items = [item for item in backend.ui.items if item.native_key != "editor"] + [
        replace(original, native_key="editor-a"), replace(original, native_key="editor-b")]

    result = registry.execute("computer.ui.focus", {"element_ref": editor["element_ref"]})

    assert result.error_category == "AMBIGUOUS_ELEMENT_REFERENCE"


def test_element_descriptor_never_retains_raw_native_uia_object(computer_stack):
    backend, _journal, _registry = computer_stack
    controller = ComputerController(backend, backend.ui)
    window_ref = controller.windows()["windows"][1]["window_ref"]
    controller.inspect_ui(window_ref)

    assert all(isinstance(binding.native_key, (str, tuple))
               for binding in controller._elements_by_ref.values())


def test_contextual_tool_selection_is_deterministic_and_registry_authoritative(computer_stack):
    _backend, _journal, registry = computer_stack
    normal = select_capabilities("Bonjour Nova")
    desktop = select_capabilities("Écris dans cette fenêtre")
    assert "computer.ui.set_value" not in normal and "computer.ui.set_value" in desktop
    assert {item["function"]["name"] for item in tool_definitions(registry, desktop)} <= EXPOSED_CAPABILITIES


def test_french_computer_request_crosses_contextual_adapter_and_calls_windows(computer_stack):
    _backend, journal, registry = computer_stack
    definitions = tool_definitions(registry)
    adapter = NovaEngineAdapter(tool_definitions=definitions)

    class Manager:
        turn = 0
        calls = []

        def _chat(self, **kwargs):
            self.calls.append(kwargs)
            self.turn += 1
            if self.turn == 1:
                names = {item["function"]["name"] for item in kwargs["tools"]}
                assert "computer.windows" in names
                assert "computer.ui.set_value" not in names
                return {"message": {"role": "assistant", "tool_calls": [{
                    "function": {"name": "computer.windows", "arguments": {}}
                }]}}
            return {"message": {"role": "assistant", "content": "Fenêtres observées."}}

        @staticmethod
        def _assistant_message(response):
            return response["message"]

    manager = Manager()
    adapter._manager = manager
    outcome = AgentLoop(adapter, registry, journal).run(
        "Quelles fenêtres sont actuellement ouvertes sur mon ordinateur ?",
        [], "agent", Event(), conversation_id="c", generation_id="g", notify=lambda *_: None,
    )

    assert outcome.status == "success" and outcome.answer == "Fenêtres observées."
    assert any(event.capability_id == "computer.windows" for event in journal.recent())
    assert len(manager.calls) == 2
