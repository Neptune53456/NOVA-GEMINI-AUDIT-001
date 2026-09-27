from io import BytesIO

from PIL import Image

from nova_api.capabilities import build_default_registry
from nova_api.computer import ComputerController, NativeUIElement, NativeWindow
from nova_api.journal import EventJournal
from nova_api.visual import VisualController


class Desktop:
    def windows(self):
        return [NativeWindow(10, 22, "demo.exe", "Demo", "normal", (0, 0, 640, 480))]
    def active_window_id(self): return 10
    def displays(self): return [{"index": 0, "x": 0, "y": 0, "width": 640, "height": 480, "primary": True}]
    def focus(self, native_id): return True
    def minimize(self, native_id): return True
    def restore(self, native_id): return True


class UI:
    def __init__(self):
        self.items = [
            NativeUIElement("root", "window", "Demo", actions=("focus",)),
            NativeUIElement("save", "button", "Enregistrer", "root", actions=("invoke",), path=(0,)),
            NativeUIElement("cancel", "button", "Annuler", "root", actions=("invoke",), path=(1,)),
            NativeUIElement("field", "edit", "Nom", "root", editable=True, read_only=False,
                            actions=("focus", "set_value"), path=(2,)),
        ]
    def elements(self, native_window_id, *, depth, max_elements): return self.items[:max_elements]
    def invoke(self, native_key): return True
    def focus(self, native_key): return True
    def set_value(self, native_key, value): return True
    def toggle(self, native_key): return True
    def select(self, native_key): return True


class Capture:
    def capture(self, region):
        out = BytesIO(); Image.new("RGB", (region[2]-region[0], region[3]-region[1]), "white").save(out, "PNG")
        return out.getvalue()


class Vision:
    def __init__(self, answer="candidate:c1"):
        self.answer = answer
        self.calls = []
    def analyze(self, png_bytes, prompt, *, timeout_seconds=30.0):
        self.calls.append((png_bytes, prompt))
        return {"summary": self.answer, "confidence": 0.75, "provider": "mock", "model": "vision"}


def _stack(tmp_path, *, answer="candidate:c1"):
    computer = ComputerController(Desktop(), UI())
    vision = Vision(answer)
    visual = VisualController(computer, capture_backend=Capture(), analyzer=vision)
    registry = build_default_registry(EventJournal(tmp_path / "journal.sqlite3"), project_root=tmp_path,
                                      computer=computer, visual=visual)
    window_ref = registry.execute("computer.windows").result["windows"][0]["window_ref"]
    return registry, window_ref, vision


def test_perception_ground_prefers_unique_uia_match_without_vision(tmp_path):
    registry, window_ref, vision = _stack(tmp_path)
    result = registry.execute("computer.perception.ground", {"window_ref": window_ref, "query": "bouton Enregistrer"})
    assert result.status == "success"
    assert result.result["name"] == "Enregistrer"
    assert result.result["provenance"] == "SEMANTIC_UIA"
    assert result.result["vision_used"] is False
    assert vision.calls == []


def test_perception_ground_uses_vision_to_break_ambiguity(tmp_path):
    registry, window_ref, vision = _stack(tmp_path, answer="candidate:c1")
    image_ref = registry.execute("computer.visual.capture", {"target_type": "window", "window_ref": window_ref}).result["image_ref"]
    result = registry.execute("computer.perception.ground", {
        "window_ref": window_ref, "query": "le bouton en bas", "image_ref": image_ref,
    })
    assert result.status == "success"
    assert result.result["provenance"] == "FUSED_UIA_VISION"
    assert result.result["vision_used"] is True
    assert vision.calls


def test_perception_ground_rejects_visual_scope_mismatch(tmp_path):
    registry, window_ref, _vision = _stack(tmp_path)
    image_ref = registry.execute("computer.visual.capture", {"target_type": "primary_display"}).result["image_ref"]
    result = registry.execute("computer.perception.ground", {
        "window_ref": window_ref, "query": "le bouton en bas", "image_ref": image_ref,
    })
    assert result.status == "error"
    assert result.error_category == "VISUAL_UIA_SCOPE_MISMATCH"

def test_grounding_returns_structured_uncertainty_evidence(tmp_path):
    registry, window_ref, _vision = _stack(tmp_path)
    result = registry.execute("computer.perception.ground", {"window_ref": window_ref, "query": "bouton Enregistrer"})
    assert result.status == "success"
    assert result.result["source_types"] == ["UIA"]
    assert 0 <= result.result["confidence"] <= 1
    assert result.result["ambiguity_score"] == 0.0
    assert result.result["evidence"]
