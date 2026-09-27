from io import BytesIO

from PIL import Image

from nova_api.application_memory import ApplicationMemory
from nova_api.capabilities import build_default_registry
from nova_api.computer import ComputerController, NativeUIElement, NativeWindow
from nova_api.journal import EventJournal
from nova_api.local_ocr import OcrSpan
from nova_api.visual import VisualController


class Desktop:
    def windows(self):
        return [NativeWindow(10, 22, "demo.exe", "Demo - Editor", "normal", (100, 100, 700, 500))]
    def active_window_id(self): return 10
    def displays(self): return [{"index": 0, "x": 0, "y": 0, "width": 1200, "height": 800, "primary": True}]
    def focus(self, native_id): return True
    def minimize(self, native_id): return True
    def restore(self, native_id): return True


class UI:
    def __init__(self):
        self.items = [
            NativeUIElement("root", "window", "Demo", actions=("focus",), bounds=(100, 100, 700, 500)),
            NativeUIElement("a", "button", "Action", "root", actions=("invoke",), path=(0,), bounds=(140, 400, 260, 450)),
            NativeUIElement("b", "button", "Action", "root", actions=("invoke",), path=(1,), bounds=(500, 400, 650, 450)),
        ]
    def elements(self, native_window_id, *, depth, max_elements): return self.items[:max_elements]
    def invoke(self, native_key): return True
    def focus(self, native_key): return True
    def set_value(self, native_key, value): return True
    def toggle(self, native_key): return True
    def select(self, native_key): return True


class Capture:
    def __init__(self): self.calls = 0
    def capture(self, region):
        self.calls += 1
        out = BytesIO()
        Image.new("RGB", (region[2] - region[0], region[3] - region[1]), "white").save(out, "PNG")
        return out.getvalue()


class OCR:
    def __init__(self, spans=None, available=True):
        self.spans = spans or []
        self.available = available
        self.calls = 0
    def availability(self): return {"available": self.available, "backend": "mock", "reason": None if self.available else "missing"}
    def extract(self, png_bytes, *, timeout_seconds=12.0):
        self.calls += 1
        return list(self.spans)


class Vision:
    def __init__(self, answer="candidate:c0"):
        self.answer = answer
        self.calls = 0
    def analyze(self, png_bytes, prompt, *, timeout_seconds=30.0):
        self.calls += 1
        return {"summary": self.answer, "confidence": .7, "provider": "mock", "model": "vision"}


def _visual(tmp_path, *, spans=None, ocr_available=True):
    computer = ComputerController(Desktop(), UI())
    capture = Capture(); vision = Vision(); ocr = OCR(spans=spans, available=ocr_available)
    memory = ApplicationMemory(tmp_path / "apps.sqlite3")
    visual = VisualController(computer, capture_backend=capture, analyzer=vision, ocr_backend=ocr,
                              application_memory=memory)
    return computer, visual, capture, vision, ocr, memory


def test_ocr_precedes_vision_and_auto_captures_window(tmp_path):
    # Window starts at (100,100). Second button is x=500..650/y=400..450,
    # therefore OCR coordinates relative to the capture are around x=400/y=300.
    spans = [OcrSpan("Enregistrer", .96, (415, 310, 90, 24))]
    computer, visual, capture, vision, ocr, _memory = _visual(tmp_path, spans=spans)
    window_ref = computer.windows()["windows"][0]["window_ref"]
    grounded = visual.ground(window_ref=window_ref, query="Enregistrer")
    assert grounded["provenance"] == "FUSED_UIA_OCR"
    assert grounded["source_types"] == ["UIA", "OCR"]
    assert grounded["ocr_used"] is True and grounded["vision_used"] is False
    assert grounded["structural_fingerprint"]
    assert capture.calls == 1 and ocr.calls == 1 and vision.calls == 0


def test_ocr_unavailable_degrades_to_vision_without_failure(tmp_path):
    computer, visual, _capture, vision, ocr, _memory = _visual(tmp_path, ocr_available=False)
    window_ref = computer.windows()["windows"][0]["window_ref"]
    grounded = visual.ground(window_ref=window_ref, query="le bouton a droite")
    assert grounded["provenance"] == "FUSED_UIA_VISION"
    assert grounded["vision_used"] is True
    assert ocr.calls == 0 and vision.calls == 1


def test_application_memory_retrieves_related_intent_and_penalizes_failures(tmp_path):
    memory = ApplicationMemory(tmp_path / "apps.sqlite3")
    app = memory.app_identity(executable="editor.exe", app_name="Editor")
    for _ in range(3):
        memory.record(app_identity=app, intent="save the current document", target_label="Save",
                      control_type="button", structural_fingerprint="save-button", action_type="invoke", success=True)
    hints = memory.hints(app_identity=app, intent="save document")
    assert hints and hints[0].structural_fingerprint == "save-button"
    assert hints[0].intent_similarity > 0
    before = hints[0].confidence
    for _ in range(4):
        memory.record(app_identity=app, intent="save the current document", target_label="Save",
                      control_type="button", structural_fingerprint="save-button", action_type="invoke", success=False)
    after = memory.hints(app_identity=app, intent="save document")[0].confidence
    assert after < before


def test_registry_exposes_local_ocr_as_observation_only_capability(tmp_path):
    computer, visual, _capture, _vision, _ocr, memory = _visual(tmp_path, ocr_available=False)
    registry = build_default_registry(EventJournal(tmp_path / "events.sqlite3"), project_root=tmp_path,
                                      computer=computer, visual=visual)
    window_ref = registry.execute("computer.windows").result["windows"][0]["window_ref"]
    image_ref = registry.execute("computer.visual.capture", {"target_type": "window", "window_ref": window_ref}).result["image_ref"]
    result = registry.execute("computer.visual.ocr", {"image_ref": image_ref})
    assert result.status == "success" and result.verified
    assert result.result["available"] is False
    assert registry.application_memory is memory


def test_risk_context_contains_safe_structural_fingerprint(tmp_path):
    computer, _visual_controller, _capture, _vision, _ocr, _memory = _visual(tmp_path)
    window_ref = computer.windows()["windows"][0]["window_ref"]
    ui = computer.inspect_ui(window_ref)
    element_ref = next(item["element_ref"] for item in ui["elements"] if item["role"] == "button")
    context = computer.risk_context("computer.ui.invoke", {"element_ref": element_ref})
    assert context["application"] == "demo.exe"
    assert isinstance(context["structural_fingerprint"], str)
    assert len(context["structural_fingerprint"]) == 24
    assert "native" not in repr(context).casefold()


def test_deliberation_provider_failure_degrades_safely():
    from nova_api.deliberation import DeliberationEngine
    attempts = []
    def broken(role, prompt, *, timeout_seconds):
        attempts.append(role)
        raise RuntimeError("provider down")
    result = DeliberationEngine(broken, max_calls=3, max_seconds=5).deliberate(
        objective="recover", evidence="verification failed", candidate_strategy="reobserve",
        risk_level="high", uncertainty_reasons=("verification_failure",),
    )
    assert result.calls == 3 and result.failed_calls == 3
    assert result.recommendation == "reobserve"
    assert result.require_more_observation is True
    assert result.confidence_band == "low"


def test_intelligence_benchmark_does_not_invent_token_totals():
    from nova_api.intelligence_benchmark import IntelligenceBenchmark
    bench = IntelligenceBenchmark()
    baseline, candidate = bench.run_pair(
        scenario_id="ambiguous-ui",
        baseline=lambda: {"success": False, "verified": False, "model_calls": 1},
        candidate=lambda: {"success": True, "verified": True, "model_calls": 3, "tokens_total": 120},
    )
    summary = bench.summarize([baseline, candidate])
    assert summary["count"] == 2
    assert summary["authoritative_token_coverage"] == .5
    assert summary["tokens_total"] is None
    assert summary["verified_rate"] == .5


def test_perception_observability_records_source_without_content(tmp_path):
    computer, visual, _capture, _vision, _ocr, _memory = _visual(tmp_path, ocr_available=False)
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path, computer=computer, visual=visual)
    window_ref = registry.execute("computer.windows").result["windows"][0]["window_ref"]
    result = registry.execute("computer.perception.ground", {"window_ref": window_ref, "query": "Action"})
    assert result.status == "success"
    event = next(event for event in journal.recent(limit=10) if event.capability_id == "computer.perception.ground")
    assert event.structural_fingerprint["provenance"] == result.result["provenance"]
    assert "query" not in event.structural_fingerprint
    assert "content" not in event.structural_fingerprint


def test_application_memory_breaks_semantic_tie_without_vision(tmp_path):
    computer, visual, _capture, vision, _ocr, memory = _visual(tmp_path, ocr_available=False)
    window_ref = computer.windows()["windows"][0]["window_ref"]
    ui = computer.inspect_ui(window_ref)
    buttons = [item for item in ui["elements"] if item["role"] == "button"]
    assert len(buttons) == 2
    window, _ = computer.resolve_window(window_ref)
    app = memory.app_identity(executable=window.application, app_name=window.application,
                              title_family=window.title.rsplit(" - ", 1)[-1])
    preferred = buttons[1]
    memory.record(app_identity=app, intent="Action", target_label="Action", control_type="button",
                  structural_fingerprint=visual._structural_fingerprint(preferred),
                  action_type="invoke", success=True)
    grounded = visual.ground(window_ref=window_ref, query="Action")
    assert grounded["element_ref"] == preferred["element_ref"]
    assert grounded["provenance"] == "SEMANTIC_UIA"
    assert "APPLICATION_MEMORY" in grounded["source_types"]
    assert vision.calls == 0
