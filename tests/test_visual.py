import json
from io import BytesIO
from threading import Event

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from nova_api.agent_loop import AgentLoop, select_capabilities
from nova_api.app import create_app
from nova_api.capabilities import build_default_registry
from nova_api.computer import ComputerController, NativeWindow
from nova_api.journal import EventJournal
from nova_api.visual import RouterVisionAnalyzer, VisualController
from self_improvement.multimodal import ImagePart, TextPart


class Desktop:
    def __init__(self):
        self.items = [NativeWindow(7, 3, "notepad.exe", "Notes", "normal", (-100, 20, 500, 420))]

    def windows(self): return list(self.items)
    def active_window_id(self): return 7
    def displays(self): return [
        {"index": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "primary": True},
        {"index": 1, "x": -1280, "y": 0, "width": 1280, "height": 1024, "primary": False},
    ]
    def focus(self, native_id): return True
    def minimize(self, native_id): return True
    def restore(self, native_id): return True


class Capture:
    def __init__(self): self.regions = []
    def capture(self, region):
        self.regions.append(region)
        output = BytesIO()
        Image.new("RGB", (region[2] - region[0], region[3] - region[1]), "white").save(output, "PNG")
        return output.getvalue()


class Analyzer:
    def __init__(self): self.calls = []
    def analyze(self, data, prompt, *, timeout_seconds=30):
        self.calls.append((data, prompt))
        return {"summary": "Une fenetre Bloc-notes.", "confidence": 0.8, "provider": "mock", "model": "vision"}


class UI:
    def elements(self, native_window_id, *, depth, max_elements): return []


@pytest.fixture
def visual_stack(tmp_path):
    desktop, capture, analyzer = Desktop(), Capture(), Analyzer()
    computer = ComputerController(desktop, UI())
    visual = VisualController(computer, capture_backend=capture, analyzer=analyzer)
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path, computer=computer, visual=visual)
    return desktop, capture, analyzer, visual, journal, registry


def test_display_topology_primary_multiple_and_negative_coordinates(visual_stack):
    _desktop, _capture, _analyzer, _visual, _journal, registry = visual_stack
    result = registry.execute("computer.visual.displays").result
    assert len(result["displays"]) == 2
    assert result["displays"][0]["primary"] is True
    assert result["displays"][1]["x"] == -1280
    assert all(item["display_ref"].startswith("display_") for item in result["displays"])


def test_primary_explicit_display_and_window_capture_share_provenance(visual_stack):
    _desktop, capture, _analyzer, _visual, _journal, registry = visual_stack
    topology = registry.execute("computer.visual.displays").result
    primary = registry.execute("computer.visual.capture", {"target_type": "primary_display"}).result
    second = registry.execute("computer.visual.capture", {"target_type": "display", "display_ref": topology["displays"][1]["display_ref"]}).result
    window_ref = registry.execute("computer.windows").result["windows"][0]["window_ref"]
    window = registry.execute("computer.visual.capture", {"target_type": "window", "window_ref": window_ref}).result
    ui_generation = registry.execute("computer.ui.inspect", {"window_ref": window_ref}).result["observation_generation"]
    assert capture.regions == [(0, 0, 1920, 1080), (-1280, 0, 0, 1024), (-100, 20, 500, 420)]
    assert window["window_ref"] == window_ref and window["observation_generation"] == ui_generation
    assert primary["image_ref"].startswith("image_") and second["scope"] == "display"


def test_image_refs_expire_and_registry_is_bounded(visual_stack):
    desktop, capture, analyzer, _visual, _journal, _registry = visual_stack
    now = [0.0]
    visual = VisualController(ComputerController(desktop), capture_backend=capture, analyzer=analyzer,
                              ttl=2, max_images=2, clock=lambda: now[0])
    refs = [visual.capture(target_type="primary_display")["image_ref"] for _ in range(3)]
    with pytest.raises(Exception, match="STALE_IMAGE_REFERENCE"): visual.inspect(refs[0])
    now[0] = 3
    with pytest.raises(Exception, match="STALE_IMAGE_REFERENCE"): visual.inspect(refs[-1])


def test_stale_image_fails_before_provider_materialization(visual_stack):
    desktop, capture, analyzer, _visual, _journal, _registry = visual_stack
    now = [0.0]
    visual = VisualController(
        ComputerController(desktop), capture_backend=capture, analyzer=analyzer,
        ttl=1, clock=lambda: now[0],
    )
    image_ref = visual.capture(target_type="primary_display")["image_ref"]
    now[0] = 2

    with pytest.raises(Exception, match="STALE_IMAGE_REFERENCE"):
        visual.analyze(image_ref)
    assert analyzer.calls == []


def test_invalid_and_stale_window_capture_fail_structurally(visual_stack):
    desktop, _capture, _analyzer, _visual, _journal, registry = visual_stack
    invalid = registry.execute("computer.visual.capture", {"target_type": "window", "window_ref": "bad"})
    assert invalid.error_category == "WINDOW_NOT_FOUND"
    window_ref = registry.execute("computer.windows").result["windows"][0]["window_ref"]
    desktop.items = []
    stale = registry.execute("computer.visual.capture", {"target_type": "window", "window_ref": window_ref})
    assert stale.error_category == "STALE_WINDOW_REFERENCE"


def test_visual_analysis_is_compact_and_pixels_never_enter_journal(visual_stack):
    _desktop, _capture, analyzer, _visual, journal, registry = visual_stack
    image_ref = registry.execute("computer.visual.capture", {"target_type": "primary_display"}).result["image_ref"]
    result = registry.execute("computer.visual.analyze", {"image_ref": image_ref, "prompt": "Que vois-tu ?"})
    assert result.result["provenance"] == "VISUAL_MODEL" and result.result["summary"]
    assert analyzer.calls and analyzer.calls[0][0].startswith(b"\x89PNG")
    persisted = repr(journal.recent())
    assert "base64" not in persisted and "Une fenetre" not in persisted and ".png" not in persisted


def test_visual_intent_selection_is_explicit_and_accent_insensitive():
    primary = select_capabilities("Que vois-tu actuellement sur mon écran ?")
    named = select_capabilities("Look at this window")
    active = select_capabilities("Que vois-tu dans la fenêtre active ?")
    display = select_capabilities("Look at the second monitor")
    assert "computer.visual.capture" in primary
    assert not {"computer.windows", "computer.active_window", "computer.visual.displays"} & primary
    assert "computer.windows" in named and "computer.active_window" not in named
    assert "computer.visual.displays" not in named
    assert "computer.active_window" in active and "computer.windows" not in active
    assert "computer.visual.displays" in display
    assert "computer.visual.capture" not in select_capabilities("Bonjour Nova")
    assert "computer.visual.capture" not in select_capabilities("Lis le fichier README.md")


def test_visual_capture_is_discovery_and_does_not_consume_mutation_budget(visual_stack):
    _desktop, _capture, _analyzer, _visual, journal, registry = visual_stack
    class Model:
        turn = 0
        def agent_turn(self, messages, tools, *, timeout_seconds):
            self.turn += 1
            if self.turn <= 2:
                return {"message": {"tool_calls": [{"function": {"name": "computer.visual.capture", "arguments": {"target_type": "primary_display"}}}]}}
            return {"message": {"content": "Termine."}}
    outcome = AgentLoop(Model(), registry, journal, max_actions=0, max_discoveries=2).run(
        "Regarde mon ecran", [], "agent", Event(), conversation_id="c", generation_id="g", notify=lambda *_: None)
    assert outcome.status == "success"
    assert any(event.error_category == "repeated_observation" for event in journal.recent())


def test_failed_visual_discovery_still_consumes_budget(visual_stack):
    _desktop, _capture, _analyzer, _visual, journal, registry = visual_stack
    class Model:
        turn = 0
        def agent_turn(self, messages, tools, *, timeout_seconds):
            self.turn += 1
            arguments = ({"target_type": "display", "display_ref": "display_unknown"}
                         if self.turn == 1 else {"target_type": "primary_display"})
            return {"message": {"tool_calls": [{"function": {
                "name": "computer.visual.capture", "arguments": arguments,
            }}]}}

    outcome = AgentLoop(Model(), registry, journal, max_discoveries=1).run(
        "Regarde mon ecran", [], "agent", Event(), conversation_id="c", generation_id="g",
        notify=lambda *_: None,
    )
    assert outcome.status == "error" and outcome.error_category == "discovery_budget_exceeded"
    assert any(event.error_category == "DISPLAY_NOT_FOUND" for event in journal.recent())


def test_agent_loop_window_capture_then_visual_analysis(visual_stack):
    _desktop, _capture, _analyzer, _visual, journal, registry = visual_stack
    class Model:
        turn = 0
        requested = []
        def agent_turn(self, messages, tools, *, timeout_seconds):
            self.turn += 1
            available = {item["function"]["name"] for item in tools}
            assert "computer.active_window" not in available
            assert "computer.visual.displays" not in available
            if self.turn == 1:
                name, arguments = "computer.windows", {}
            elif self.turn == 2:
                import json
                window_ref = json.loads(messages[-1]["content"])["windows"][0]["window_ref"]
                name, arguments = "computer.visual.capture", {"target_type": "window", "window_ref": window_ref}
            elif self.turn == 3:
                import json
                image_ref = json.loads(messages[-1]["content"])["image_ref"]
                name, arguments = "computer.visual.analyze", {"image_ref": image_ref}
            else:
                return {"message": {"content": "Une fenetre Bloc-notes est affichee."}}
            self.requested.append((name, arguments))
            return {"message": {"tool_calls": [{"function": {"name": name, "arguments": arguments}}]}}
    outcome = AgentLoop(Model(), registry, journal).run(
        "Regarde la fenetre Bloc-notes et dis-moi ce qui est affiche.", [], "agent", Event(),
        conversation_id="c", generation_id="g", notify=lambda *_: None)
    assert outcome.status == "success" and "Bloc-notes" in outcome.answer
    assert [name for name, _arguments in Model.requested] == [
        "computer.windows", "computer.visual.capture", "computer.visual.analyze",
    ]
    captured_ref = Model.requested[1][1]["window_ref"]
    image_ref = Model.requested[2][1]["image_ref"]
    binding = _visual.inspect(image_ref)
    assert binding["window_ref"] == captured_ref and binding["scope"] == "window"


def test_capture_target_contract_rejects_conflicts_and_distinguishes_errors(visual_stack):
    _desktop, _capture, _analyzer, _visual, _journal, registry = visual_stack
    topology = registry.execute("computer.visual.displays").result

    with pytest.raises(ValueError, match="invalid_arguments"):
        registry.execute("computer.visual.capture", {
            "target_type": "window", "window_ref": "window_x", "display_ref": "display_x",
        })
    with pytest.raises(ValueError, match="invalid_arguments"):
        registry.execute("computer.visual.capture", {"window_ref": "window_x"})

    invalid_display = registry.execute("computer.visual.capture", {
        "target_type": "display", "display_ref": "display_unknown",
    })
    assert invalid_display.error_category == "DISPLAY_NOT_FOUND"
    valid_display = registry.execute("computer.visual.capture", {
        "target_type": "display", "display_ref": topology["displays"][1]["display_ref"],
    })
    assert valid_display.status == "success" and valid_display.result["scope"] == "display"


def test_split_tool_and_vision_providers_complete_visual_flow(visual_stack):
    _desktop, _capture, analyzer, _visual, journal, registry = visual_stack

    class ToolOnlyPlanner:
        supports_tools = True
        supports_vision = False
        turn = 0

        def agent_turn(self, messages, tools, *, timeout_seconds):
            self.turn += 1
            assert tools
            if self.turn == 1:
                name, arguments = "computer.windows", {}
            elif self.turn == 2:
                name = "computer.visual.capture"
                arguments = {"target_type": "window", "window_ref": json.loads(messages[-1]["content"])["windows"][0]["window_ref"]}
            elif self.turn == 3:
                name = "computer.visual.analyze"
                arguments = {"image_ref": json.loads(messages[-1]["content"])["image_ref"]}
            else:
                assert json.loads(messages[-1]["content"])["provenance"] == "VISUAL_MODEL"
                return {"message": {"content": "Bloc-notes affiche une note."}}
            return {"message": {"tool_calls": [{"id": f"call-{self.turn}", "function": {
                "name": name, "arguments": arguments,
            }}]}}

    outcome = AgentLoop(ToolOnlyPlanner(), registry, journal).run(
        "Regarde la fenetre Bloc-notes et dis-moi ce qui est affiche.", [], "agent", Event(),
        conversation_id="c", generation_id="g", notify=lambda *_: None,
    )

    assert outcome.status == "success"
    assert analyzer.calls


def test_router_vision_analyzer_requires_vision_without_tools(monkeypatch):
    captured = {}

    def fake_chat(messages, **kwargs):
        captured.update(kwargs)
        return {"message": {"content": "Une note."}}

    import model_router
    monkeypatch.setattr(model_router, "chat", fake_chat)
    RouterVisionAnalyzer().analyze(b"png", "Decris")

    assert captured["required_capabilities"] == {"vision"}
    assert "tools" not in captured


def test_router_vision_analyzer_uses_provider_neutral_image_parts(monkeypatch):
    captured = {}

    def fake_chat(messages, **_kwargs):
        captured["messages"] = messages
        return {"message": {"content": "NOVA VISION 7391"}}

    import model_router
    monkeypatch.setattr(model_router, "chat", fake_chat)
    RouterVisionAnalyzer().analyze(b"real-png-bytes", "Lis le texte")

    parts = captured["messages"][0]["content"]
    assert parts == [TextPart("Lis le texte"), ImagePart("image/png", b"real-png-bytes")]
    assert not any(isinstance(part, str) and ".png" in part for part in parts)


def test_openai_adapter_materializes_canonical_multimodal_request(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self): return None
        def json(self): return {"choices": [{"message": {"content": "texte lu"}}]}

    def fake_post(*_args, **kwargs):
        captured.update(kwargs["json"])
        return Response()

    import model_router
    monkeypatch.setattr(model_router.httpx, "post", fake_post)
    response = model_router._call_omniroute_model(
        "mock/vision", [{"role": "user", "content": [
            TextPart("Lis"), ImagePart("image/png", b"pixels"),
        ]}], timeout_seconds=1,
    )

    content = captured["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Lis"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,cGl4ZWxz"
    assert response["message"]["content"] == "texte lu"


def test_router_vision_analyzer_reports_missing_vision_provider(monkeypatch):
    import model_router

    def unavailable(*_args, **_kwargs):
        raise model_router.ModelRouterError(
            "no_capable_provider", "none", details={"required_capabilities": ["vision"]},
        )

    monkeypatch.setattr(model_router, "chat", unavailable)

    with pytest.raises(Exception, match="NO_VISION_CAPABLE_PROVIDER"):
        RouterVisionAnalyzer().analyze(b"png", "Decris")


@pytest.mark.parametrize("kind", ["invalid_request", "invalid_response"])
def test_router_vision_analyzer_reports_protocol_errors(monkeypatch, kind):
    import model_router

    def malformed(*_args, **_kwargs):
        raise model_router.ModelRouterError(kind, "malformed")

    monkeypatch.setattr(model_router, "chat", malformed)
    with pytest.raises(Exception, match="VISION_PROTOCOL_ERROR"):
        RouterVisionAnalyzer().analyze(b"png", "Decris")


def test_router_vision_analyzer_reports_exhausted_provider(monkeypatch):
    import model_router

    def failed(*_args, **_kwargs):
        raise model_router.ModelRouterError(
            "routes_failed", "failed", details={"required_capabilities": ["vision"]},
        )

    monkeypatch.setattr(model_router, "chat", failed)
    with pytest.raises(Exception, match="VISION_PROVIDER_UNAVAILABLE"):
        RouterVisionAnalyzer().analyze(b"png", "Decris")


def test_router_vision_analyzer_reports_mixed_route_exhaustion_as_provider_unavailable(monkeypatch):
    import model_router

    def failed(*_args, **_kwargs):
        raise model_router.ModelRouterError(
            "routes_failed", "failed", details={"required_capabilities": ["vision"], "route_diagnostics": [
                {"attempt_index": 1, "reason": "TRANSIENT_SERVER_ERROR"},
                {"attempt_index": 2, "reason": "INVALID_REQUEST"},
                {"attempt_index": 3, "reason": "TRANSIENT_SERVER_ERROR"},
            ]},
        )

    monkeypatch.setattr(model_router, "chat", failed)
    with pytest.raises(Exception, match="VISION_PROVIDER_UNAVAILABLE"):
        RouterVisionAnalyzer().analyze(b"png", "Decris")


def test_agent_loop_finalizes_after_terminal_visual_provider_failure(visual_stack):
    _desktop, _capture, _analyzer, visual, journal, registry = visual_stack

    class UnavailableAnalyzer:
        def analyze(self, *_args, **_kwargs):
            from nova_api.computer import ComputerError
            raise ComputerError("VISION_PROVIDER_UNAVAILABLE")

    visual.analyzer = UnavailableAnalyzer()

    class Model:
        turn = 0
        analyze_requests = 0

        def agent_turn(self, messages, tools, *, timeout_seconds):
            self.turn += 1
            if self.turn == 1:
                name, arguments = "computer.windows", {}
            elif self.turn == 2:
                window_ref = json.loads(messages[-1]["content"])["windows"][0]["window_ref"]
                name, arguments = "computer.visual.capture", {"target_type": "window", "window_ref": window_ref}
            else:
                self.analyze_requests += 1
                image_ref = next(
                    json.loads(message["content"])["image_ref"] for message in reversed(messages)
                    if message.get("role") == "tool" and "image_ref" in json.loads(message["content"])
                )
                name, arguments = "computer.visual.analyze", {"image_ref": image_ref}
            return {"message": {"tool_calls": [{"id": f"call-{self.turn}", "function": {
                "name": name, "arguments": arguments,
            }}]}}

    model = Model()
    outcome = AgentLoop(model, registry, journal).run(
        "Regarde la fenetre Bloc-notes et dis-moi ce qui est affiche.", [], "agent", Event(),
        conversation_id="c", generation_id="g", notify=lambda *_: None,
    )

    assert outcome.status == "success"
    assert "indisponible" in outcome.answer.casefold()
    assert model.analyze_requests == 1
    assert not any(event.error_category == "discovery_budget_exceeded" for event in journal.recent())


def test_read_only_displays_api(visual_stack):
    _desktop, _capture, _analyzer, _visual, journal, registry = visual_stack
    response = TestClient(create_app(journal=journal, capability_registry=registry)).get("/api/v1/computer/displays")
    assert response.status_code == 200 and len(response.json()["displays"]) == 2


def test_router_selects_only_explicitly_vision_capable_model(monkeypatch):
    import model_router
    from self_improvement.brain_pool import RoutingDecision, ScoredCandidate
    from self_improvement.model_catalog import ModelDescriptor, ModelPool
    model = ModelDescriptor("mock/vision", provider="mock", supports_vision=True, pools={ModelPool.VISION})
    candidate = ScoredCandidate(model, 1.0)
    class Catalog:
        def models(self): return [model]
    class Router:
        memory = None
        def route(self, profile, models):
            assert profile.vision_required is True
            return RoutingDecision(profile, candidate, [])
    monkeypatch.setenv("OMNIROUTE_API_KEY", "configured")
    monkeypatch.setenv("OMNIROUTE_ENABLED", "1")
    monkeypatch.setattr(model_router, "_v7_components", lambda: (Catalog(), Router()))
    monkeypatch.setattr(model_router, "_is_provider_available", lambda scope: True)
    monkeypatch.setattr(model_router, "_call_omniroute_model", lambda *args, **kwargs: {"message": {"content": "vu"}})
    response = model_router.chat([{"role": "user", "content": "image"}], required_capabilities={"vision"})
    assert response["message"]["content"] == "vu"
    assert response["_meta"]["requested_capabilities"]["vision"] is True
    assert response["_meta"]["capability_class"] == "vision"
    assert response["_meta"]["phase"] == "visual_analysis"


def test_router_rejects_text_only_model_for_visual_request(monkeypatch):
    import model_router
    from self_improvement.brain_pool import RoutingDecision, ScoredCandidate
    from self_improvement.model_catalog import ModelDescriptor
    model = ModelDescriptor("mock/text", provider="mock", supports_vision=False)
    candidate = ScoredCandidate(model, 1.0)
    class Catalog:
        def models(self): return [model]
    class Router:
        memory = None
        def route(self, profile, models): return RoutingDecision(profile, candidate, [])
    monkeypatch.setenv("OMNIROUTE_API_KEY", "configured")
    monkeypatch.setenv("OMNIROUTE_ENABLED", "1")
    monkeypatch.setattr(model_router, "_v7_components", lambda: (Catalog(), Router()))
    with pytest.raises(model_router.ModelRouterError) as error:
        model_router.chat([{"role": "user", "content": "image"}], required_capabilities={"vision"})
    assert error.value.kind == "no_capable_provider"
