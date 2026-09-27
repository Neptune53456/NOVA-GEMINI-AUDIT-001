"""Deterministic, semantic observation and control of the local Windows desktop."""
from __future__ import annotations

import ctypes
import hashlib
import ctypes.wintypes
import os
import platform
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

MAX_WINDOW_TITLE = 240
MAX_UI_TEXT = 240
MAX_UI_DEPTH = 8
MAX_UI_ELEMENTS = 200
MAX_TEXT_INPUT = 16_000
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class ComputerError(RuntimeError):
    """A public, sanitized computer capability failure."""


@dataclass(frozen=True)
class NativeWindow:
    native_id: int
    process_id: int | None
    application: str
    title: str
    state: str
    bounds: tuple[int, int, int, int] | None = None
    class_name: str = ""


class DesktopBackend(Protocol):
    def windows(self) -> list[NativeWindow]: ...
    def active_window_id(self) -> int | None: ...
    def displays(self) -> list[dict[str, int | bool]]: ...
    def focus(self, native_id: int) -> bool: ...
    def minimize(self, native_id: int) -> bool: ...
    def restore(self, native_id: int) -> bool: ...


@dataclass(frozen=True)
class NativeUIElement:
    """Adapter-owned UI element. ``native_key`` never crosses the controller boundary."""

    native_key: object
    role: str
    name: str = ""
    parent_key: object | None = None
    enabled: bool = True
    offscreen: bool = False
    focused: bool = False
    editable: bool | None = None
    read_only: bool | None = None
    selected: bool | None = None
    checked: bool | None = None
    password: bool = False
    value: str | None = None
    actions: tuple[str, ...] = ()
    automation_id: str = ""
    path: tuple[int, ...] = ()
    bounds: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class ElementDescriptor:
    role: str
    automation_id: str
    name: str
    password: bool
    path: tuple[int, ...]
    bounds: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class ElementBinding:
    window_ref: str
    generation: int
    descriptor: ElementDescriptor
    native_key: object
    application: str = ""
    window_title: str = ""
    window_class: str = ""


class UIAutomationBackend(Protocol):
    def elements(self, native_window_id: int, *, depth: int, max_elements: int) -> list[NativeUIElement]: ...
    def invoke(self, native_key: object) -> bool: ...
    def focus(self, native_key: object) -> bool: ...
    def set_value(self, native_key: object, value: str) -> bool: ...
    def toggle(self, native_key: object) -> bool: ...
    def select(self, native_key: object) -> bool: ...


class UnavailableUIAutomationBackend:
    """Dependency-free boundary used until a native UIA provider is available."""

    def _unavailable(self, *_args: object, **_kwargs: object) -> Any:
        raise ComputerError("UI_AUTOMATION_UNAVAILABLE")

    elements = invoke = focus = set_value = toggle = select = _unavailable


def _default_ui_backend() -> UIAutomationBackend:
    if os.name != "nt": return UnavailableUIAutomationBackend()
    try:
        from .windows_uia import ComtypesUIAutomationBackend
        return ComtypesUIAutomationBackend()
    except (ImportError, OSError, RuntimeError):
        return UnavailableUIAutomationBackend()


class WindowsDesktopBackend:
    """Small ctypes adapter. Native handles never leave this module."""

    SW_MINIMIZE = 6
    SW_RESTORE = 9

    def __init__(self) -> None:
        if os.name != "nt":
            raise ComputerError("PLATFORM_UNSUPPORTED")
        self.user32 = ctypes.windll.user32
        self.user32.GetForegroundWindow.restype = ctypes.c_void_p
        self.user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        self.user32.IsWindowVisible.restype = ctypes.c_bool
        self.user32.IsIconic.argtypes = [ctypes.c_void_p]
        self.user32.IsIconic.restype = ctypes.c_bool
        self.user32.IsZoomed.argtypes = [ctypes.c_void_p]
        self.user32.IsZoomed.restype = ctypes.c_bool
        self.user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        self.user32.SetForegroundWindow.restype = ctypes.c_bool
        self.user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        self.user32.GetClassNameW.restype = ctypes.c_int

    @staticmethod
    def _clean(value: str) -> str:
        return _CONTROL_CHARACTERS.sub(" ", value).strip()[:MAX_WINDOW_TITLE]

    def _title(self, native_id: int) -> str:
        length = int(self.user32.GetWindowTextLengthW(native_id))
        buffer = ctypes.create_unicode_buffer(min(length + 1, MAX_WINDOW_TITLE + 1))
        self.user32.GetWindowTextW(native_id, buffer, len(buffer))
        return self._clean(buffer.value)

    def _application(self, native_id: int, process_id: int) -> str:
        # PROCESS_QUERY_LIMITED_INFORMATION; the path is used only to derive a basename.
        kernel32 = ctypes.windll.kernel32
        process = kernel32.OpenProcess(0x1000, False, process_id)
        if process:
            try:
                size = ctypes.c_uint32(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                    return self._clean(Path(buffer.value).name)
            finally:
                kernel32.CloseHandle(process)
        title = self._title(native_id)
        return self._clean(title.rsplit(" - ", 1)[-1]) or "unknown"

    def _class_name(self, native_id: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(native_id, buffer, len(buffer))
        return self._clean(buffer.value)

    def windows(self) -> list[NativeWindow]:
        found: list[NativeWindow] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def visit(native_id: int, _parameter: int) -> bool:
            if not self.user32.IsWindowVisible(native_id):
                return True
            title = self._title(native_id)
            if not title:
                return True
            process_id = ctypes.c_uint32()
            self.user32.GetWindowThreadProcessId(native_id, ctypes.byref(process_id))
            state = "minimized" if self.user32.IsIconic(native_id) else (
                "maximized" if self.user32.IsZoomed(native_id) else "normal"
            )
            rect = ctypes.wintypes.RECT()
            bounds = None
            if self.user32.GetWindowRect(native_id, ctypes.byref(rect)):
                bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
            found.append(NativeWindow(int(native_id), int(process_id.value) or None,
                                      self._application(native_id, int(process_id.value)), title, state, bounds,
                                      self._class_name(native_id)))
            return True

        try:
            if not self.user32.EnumWindows(callback_type(visit), 0):
                raise ComputerError("WINDOW_ENUMERATION_FAILED")
        except ComputerError:
            raise
        except (AttributeError, OSError, TypeError, ValueError):
            raise ComputerError("WINDOW_ENUMERATION_FAILED") from None
        return found

    def active_window_id(self) -> int | None:
        try:
            value = self.user32.GetForegroundWindow()
            return int(value) if value else None
        except (AttributeError, OSError, TypeError, ValueError):
            raise ComputerError("ACTIVE_WINDOW_UNAVAILABLE") from None

    def displays(self) -> list[dict[str, int | bool]]:
        try:
            found: list[dict[str, int | bool]] = []
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_void_p)
            def visit(_monitor: int, _dc: int, rect: Any, _data: int) -> bool:
                value = rect.contents
                found.append({"index": len(found), "x": int(value.left), "y": int(value.top),
                              "width": int(value.right - value.left), "height": int(value.bottom - value.top),
                              "primary": int(value.left) == 0 and int(value.top) == 0})
                return True
            if not self.user32.EnumDisplayMonitors(0, 0, callback_type(visit), 0):
                raise OSError
            return found
        except (AttributeError, OSError, TypeError, ValueError):
            return []

    def focus(self, native_id: int) -> bool:
        return bool(self.user32.SetForegroundWindow(native_id))

    def minimize(self, native_id: int) -> bool:
        self.user32.ShowWindow(native_id, self.SW_MINIMIZE)
        return True

    def restore(self, native_id: int) -> bool:
        self.user32.ShowWindow(native_id, self.SW_RESTORE)
        return True


class ComputerController:
    """Owns opaque window references and deterministic post-action verification."""

    def __init__(self, backend: DesktopBackend | None = None,
                 ui_backend: UIAutomationBackend | None = None) -> None:
        self.backend = backend
        self.ui_backend = ui_backend or _default_ui_backend()
        self._refs_by_native_id: dict[int, tuple[int | None, str]] = {}
        self._windows_by_ref: dict[str, NativeWindow] = {}
        self._elements_by_ref: dict[str, ElementBinding] = {}
        self._generation = 0
        self._observation_signature: tuple[object, ...] | None = None
        self._lock = RLock()

    def _backend(self) -> DesktopBackend:
        if self.backend is None:
            self.backend = WindowsDesktopBackend()
        return self.backend

    def observe(self) -> dict[str, object]:
        backend = self._backend()
        windows = backend.windows()
        active_id = backend.active_window_id()
        displays = backend.displays()
        signature = (tuple((item.native_id, item.process_id, item.bounds) for item in windows),
                     tuple(tuple(sorted(item.items())) for item in displays))
        with self._lock:
            if signature != self._observation_signature:
                self._generation += 1
                self._observation_signature = signature
            live_ids = {window.native_id for window in windows}
            self._refs_by_native_id = {native_id: identity for native_id, identity in self._refs_by_native_id.items()
                                        if native_id in live_ids}
            public_windows: list[dict[str, object]] = []
            current: dict[str, NativeWindow] = {}
            for window in windows:
                identity = self._refs_by_native_id.get(window.native_id)
                if identity is None or identity[0] != window.process_id:
                    identity = (window.process_id, f"window_{uuid4().hex}")
                    self._refs_by_native_id[window.native_id] = identity
                ref = identity[1]
                current[ref] = window
                public_windows.append({"window_ref": ref, "application": window.application,
                                       "title": window.title, "state": window.state,
                                       "active": window.native_id == active_id,
                                       "bounds": ({"x": window.bounds[0], "y": window.bounds[1],
                                                   "width": window.bounds[2] - window.bounds[0],
                                                   "height": window.bounds[3] - window.bounds[1]}
                                                  if window.bounds else None)})
            self._windows_by_ref = current
        active = next((window for window in public_windows if window["active"]), None)
        value: dict[str, object] = {
            "observation_id": f"observation_{uuid4().hex}",
            "generation": self._generation,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "platform": {"system": platform.system(), "release": platform.release(),
                         "machine": platform.machine()},
            "session": {"interactive": True},
            "displays": displays,
            "windows": public_windows,
            "window_count": len(public_windows),
            "active_window": active,
        }
        return value

    def windows(self) -> dict[str, object]:
        state = self.observe()
        return {key: state[key] for key in ("observation_id", "generation", "observed_at", "window_count", "windows")}

    def active_window(self) -> dict[str, object]:
        state = self.observe()
        if state["active_window"] is None:
            raise ComputerError("ACTIVE_WINDOW_UNAVAILABLE")
        return {"observation_id": state["observation_id"], "observed_at": state["observed_at"],
                "active_window": state["active_window"]}

    def _target(self, window_ref: object) -> NativeWindow:
        if not isinstance(window_ref, str) or not window_ref.startswith("window_"):
            raise ComputerError("INVALID_WINDOW_REF")
        with self._lock:
            target = self._windows_by_ref.get(window_ref)
        if target is None:
            raise ComputerError("STALE_WINDOW_REF")
        return target

    @classmethod
    def _semantic_window_title(cls, title: str) -> str:
        value = cls._clean_ui_text(title).casefold().lstrip("* ")
        # Common document-title separators retain the semantic document/app title
        # while ignoring transient dirty markers and surrounding whitespace.
        return re.sub(r"\s+", " ", value)

    def _replacement_windows(self, binding: ElementBinding) -> list[tuple[str, NativeWindow]]:
        """Return safe top-level replacements; never use geometry as identity."""
        app = self._clean_ui_text(binding.application).casefold()
        title = self._semantic_window_title(binding.window_title)
        window_class = self._clean_ui_text(binding.window_class).casefold()
        candidates: list[tuple[str, NativeWindow]] = []
        with self._lock:
            current = list(self._windows_by_ref.items())
        for ref, window in current:
            if self._clean_ui_text(window.application).casefold() != app:
                continue
            if window_class and self._clean_ui_text(window.class_name).casefold() != window_class:
                continue
            if title and self._semantic_window_title(window.title) != title:
                continue
            candidates.append((ref, window))
        return candidates

    def resolve_window(self, window_ref: object) -> tuple[NativeWindow, int]:
        """Resolve an opaque window reference with current observation provenance."""
        self.observe()
        return self._target(window_ref), self._generation

    @staticmethod
    def _window(state: dict[str, object], window_ref: str) -> dict[str, object] | None:
        return next((item for item in state["windows"] if item["window_ref"] == window_ref), None)  # type: ignore[index,union-attr]

    def act(self, action: str, window_ref: object) -> dict[str, object]:
        before = self.observe()
        target = self._target(window_ref)
        ref = str(window_ref)
        operation = getattr(self._backend(), action)
        executed = bool(operation(target.native_id))
        after = self.observe()
        before_window, after_window = self._window(before, ref), self._window(after, ref)
        verified = False
        if executed and after_window is not None:
            verified = (bool(after_window["active"]) if action == "focus" else
                        after_window["state"] == "minimized" if action == "minimize" else
                        after_window["state"] != "minimized")
        return {
            "requested_action": f"computer.window.{action}",
            "target": {"window_ref": ref, "application": target.application},
            "execution_status": "completed" if executed else "failed",
            "verification_status": "verified" if verified else "failed",
            "observation_before": {"observation_id": before["observation_id"], "window": before_window},
            "observation_after": {"observation_id": after["observation_id"], "window": after_window},
            "error_category": None if verified else ("VERIFICATION_FAILED" if executed else "ACTION_FAILED"),
        }

    @staticmethod
    def _clean_ui_text(value: object) -> str:
        return _CONTROL_CHARACTERS.sub(" ", str(value or "")).strip()[:MAX_UI_TEXT]

    @classmethod
    def _element_descriptor(cls, element: NativeUIElement) -> ElementDescriptor:
        return ElementDescriptor(
            role=cls._clean_ui_text(element.role) or "unknown",
            automation_id=cls._clean_ui_text(element.automation_id),
            name=cls._clean_ui_text(element.name),
            password=bool(element.password),
            path=tuple(element.path),
            bounds=element.bounds,
        )

    @classmethod
    def _element_identity(cls, element: NativeUIElement) -> tuple[object, ...]:
        """Compatibility helper for tests and adapters; excludes transient native keys."""
        descriptor = cls._element_descriptor(element)
        return (descriptor.role, descriptor.automation_id, descriptor.name,
                descriptor.password, descriptor.path, descriptor.bounds)

    @staticmethod
    def _descriptor_fingerprint(descriptor: ElementDescriptor) -> str:
        # Prefer stable semantic identity.  Names may legitimately change on
        # dynamic controls, so only use the name when there is no stronger
        # automation id or structural path.
        semantic_name = "" if descriptor.automation_id or descriptor.path else descriptor.name.casefold()[:120]
        payload = "|".join((
            descriptor.role.casefold(), descriptor.automation_id.casefold(), semantic_name,
            "/".join(str(part) for part in descriptor.path),
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def _descriptor_matches(cls, descriptor: ElementDescriptor,
                            element: NativeUIElement) -> bool:
        candidate = cls._element_descriptor(element)
        if candidate.role != descriptor.role or candidate.password != descriptor.password:
            return False
        if descriptor.automation_id:
            return candidate.automation_id == descriptor.automation_id
        if descriptor.path:
            return candidate.path == descriptor.path
        return bool(descriptor.name) and candidate.name == descriptor.name and candidate.bounds == descriptor.bounds

    @classmethod
    def _recovery_candidates(cls, descriptor: ElementDescriptor,
                             elements: list[NativeUIElement]) -> list[NativeUIElement]:
        candidates = [item for item in elements
                      if cls._element_descriptor(item).role == descriptor.role and
                      bool(item.password) == descriptor.password]
        if descriptor.automation_id:
            candidates = [item for item in candidates
                          if cls._element_descriptor(item).automation_id == descriptor.automation_id]
        if len(candidates) > 1 and descriptor.path:
            candidates = [item for item in candidates if tuple(item.path) == descriptor.path]
        if len(candidates) > 1 and descriptor.name:
            candidates = [item for item in candidates
                          if cls._clean_ui_text(item.name) == descriptor.name]
        if len(candidates) > 1 and descriptor.bounds is not None:
            candidates = [item for item in candidates if item.bounds == descriptor.bounds]
        if not descriptor.automation_id and descriptor.path:
            candidates = [item for item in candidates if tuple(item.path) == descriptor.path]
        elif not descriptor.automation_id and not descriptor.path:
            candidates = [item for item in candidates if descriptor.name and
                          cls._clean_ui_text(item.name) == descriptor.name and
                          item.bounds == descriptor.bounds]
        return candidates

    @classmethod
    def _replacement_element_candidates(cls, descriptor: ElementDescriptor,
                                        elements: list[NativeUIElement]) -> list[NativeUIElement]:
        """Resolve in a new window using semantics only; old coordinates are never authority."""
        candidates = [item for item in elements
                      if cls._element_descriptor(item).role == descriptor.role and
                      bool(item.password) == descriptor.password]
        if descriptor.automation_id:
            return [item for item in candidates
                    if cls._element_descriptor(item).automation_id == descriptor.automation_id]
        if descriptor.path:
            return [item for item in candidates if tuple(item.path) == descriptor.path]
        if descriptor.name:
            return [item for item in candidates if cls._clean_ui_text(item.name) == descriptor.name]
        return []

    def _ui_call(self, method: str, *args: object, **kwargs: object) -> Any:
        try:
            return getattr(self.ui_backend, method)(*args, **kwargs)
        except ComputerError:
            raise
        except Exception:
            raise ComputerError("UI_AUTOMATION_QUERY_FAILED") from None

    def _ui_snapshot(self, window_ref: object, *, depth: object = 4,
                     max_elements: object = 80) -> tuple[NativeWindow, list[NativeUIElement]]:
        self.observe()
        window = self._target(window_ref)
        if not isinstance(depth, int) or isinstance(depth, bool) or not 0 <= depth <= MAX_UI_DEPTH:
            raise ComputerError("INVALID_UI_LIMIT")
        if not isinstance(max_elements, int) or isinstance(max_elements, bool) or not 1 <= max_elements <= MAX_UI_ELEMENTS:
            raise ComputerError("INVALID_UI_LIMIT")
        elements = self._ui_call("elements", window.native_id, depth=depth, max_elements=max_elements + 1)
        return window, elements[:max_elements]

    def inspect_ui(self, window_ref: object, *, depth: object = 4,
                   max_elements: object = 80) -> dict[str, object]:
        window, native_elements = self._ui_snapshot(window_ref, depth=depth, max_elements=max_elements)
        ref = str(window_ref)
        public: list[dict[str, object]] = []
        refs_by_key: dict[object, str] = {}
        with self._lock:
            retained = {key: value for key, value in self._elements_by_ref.items()
                        if value.window_ref != ref}
            for element in native_elements:
                descriptor = self._element_descriptor(element)
                existing = next((element_ref for element_ref, binding in self._elements_by_ref.items()
                                 if binding.window_ref == ref and binding.descriptor == descriptor), None)
                element_ref = existing or f"element_{uuid4().hex}"
                retained[element_ref] = ElementBinding(
                    ref, self._generation, descriptor, element.native_key,
                    window.application, window.title, window.class_name,
                )
                refs_by_key[element.native_key] = element_ref
            self._elements_by_ref = retained
        for element in native_elements:
            element_ref = refs_by_key[element.native_key]
            descriptor = self._element_descriptor(element)
            item: dict[str, object] = {
                "element_ref": element_ref, "role": self._clean_ui_text(element.role) or "unknown",
                "name": self._clean_ui_text(element.name), "enabled": bool(element.enabled),
                "visible": not element.offscreen, "focused": bool(element.focused),
                "supported_actions": sorted(set(element.actions)),
                "parent_ref": refs_by_key.get(element.parent_key),
                "automation_id": descriptor.automation_id,
                "target_fingerprint": self._descriptor_fingerprint(descriptor),
                "structural_path": list(descriptor.path),
                "bounds": ({"x": descriptor.bounds[0], "y": descriptor.bounds[1],
                            "width": descriptor.bounds[2] - descriptor.bounds[0],
                            "height": descriptor.bounds[3] - descriptor.bounds[1]}
                           if descriptor.bounds else None),
            }
            for key in ("editable", "read_only", "selected", "checked"):
                value = getattr(element, key)
                if value is not None: item[key] = bool(value)
            if element.password:
                item["protected"] = True
            elif element.value is not None:
                item["value"] = self._clean_ui_text(element.value)
                item["value_truncated"] = len(str(element.value)) > MAX_UI_TEXT
            public.append(item)
        return {"ui_observation_id": f"ui_observation_{uuid4().hex}", "observation_generation": self._generation,
                "window_ref": ref,
                "element_count": len(public), "elements": public,
                "truncated": len(native_elements) >= int(max_elements),
                "limits": {"depth": int(depth), "max_elements": int(max_elements)}}

    def _resolve_element(self, element_ref: object) -> tuple[str, NativeUIElement]:
        if not isinstance(element_ref, str) or not element_ref.startswith("element_"):
            raise ComputerError("ELEMENT_NOT_FOUND")
        with self._lock: binding = self._elements_by_ref.get(element_ref)
        if binding is None: raise ComputerError("STALE_ELEMENT_REFERENCE")
        self.observe()
        replaced_window = False
        try: window = self._target(binding.window_ref)
        except ComputerError as error:
            if str(error) != "STALE_WINDOW_REF":
                raise
            replacements = self._replacement_windows(binding)
            if len(replacements) > 1:
                raise ComputerError("AMBIGUOUS_WINDOW_REPLACEMENT") from None
            if not replacements:
                with self._lock: self._elements_by_ref.pop(str(element_ref), None)
                raise ComputerError("STALE_ELEMENT_REFERENCE") from None
            replacement_ref, window = replacements[0]
            replaced_window = True
            binding = ElementBinding(
                replacement_ref, self._generation, binding.descriptor, binding.native_key,
                window.application, window.title, window.class_name,
            )
        current = self._ui_call("elements", window.native_id, depth=MAX_UI_DEPTH, max_elements=MAX_UI_ELEMENTS)
        direct = [] if replaced_window else [
            item for item in current if item.native_key == binding.native_key and
            self._descriptor_matches(binding.descriptor, item)
        ]
        recovered = direct or (
            self._replacement_element_candidates(binding.descriptor, current)
            if replaced_window else self._recovery_candidates(binding.descriptor, current)
        )
        if len(recovered) > 1:
            raise ComputerError("AMBIGUOUS_ELEMENT_REFERENCE")
        if not recovered:
            with self._lock: self._elements_by_ref.pop(element_ref, None)
            raise ComputerError("STALE_ELEMENT_REFERENCE")
        element = recovered[0]
        with self._lock:
            self._elements_by_ref[str(element_ref)] = ElementBinding(
                binding.window_ref, self._generation, self._element_descriptor(element), element.native_key,
                window.application, window.title, window.class_name,
            )
        return binding.window_ref, element

    def _execute_ui_action(self, action: str, element: NativeUIElement, value: object) -> bool:
        if action == "set_value":
            return bool(self._ui_call("set_value", element.native_key, value))
        return bool(self._ui_call(action, element.native_key))


    def risk_context(self, capability_id: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
        """Return bounded semantic target metadata for deterministic risk assessment.

        This never exposes native handles or raw control values. Unknown/stale references
        intentionally produce an empty context so the static capability risk remains authoritative.
        """
        args = arguments or {}
        try:
            if isinstance(args.get("element_ref"), str):
                element_ref = str(args["element_ref"])
                # Resolve deterministically before risk assessment too.  Otherwise
                # GoalRunner would classify a safely replaceable window as ambiguous
                # and spend confirmation/replan budget before the action resolver ran.
                self._resolve_element(element_ref)
                with self._lock: binding = self._elements_by_ref.get(element_ref)
                if binding is None: return {"ambiguous": True, "target_kind": "element"}
                window = self._target(binding.window_ref)
                descriptor = binding.descriptor
                return {
                    "target_kind": "element",
                    "application": self._clean_ui_text(window.application),
                    "window_title": self._clean_ui_text(window.title),
                    "target_role": descriptor.role,
                    "target_name": descriptor.name,
                    "structural_fingerprint": self._descriptor_fingerprint(descriptor),
                    "protected": descriptor.password,
                }
            if isinstance(args.get("window_ref"), str):
                window, _generation = self.resolve_window(args["window_ref"])
                return {
                    "target_kind": "window",
                    "application": self._clean_ui_text(window.application),
                    "window_title": self._clean_ui_text(window.title),
                }
        except ComputerError:
            return {"ambiguous": True}
        return {}

    def ui_action(self, action: str, element_ref: object, *, value: object = None) -> dict[str, object]:
        if not isinstance(element_ref, str) or not element_ref.startswith("element_"):
            raise ComputerError("ELEMENT_NOT_FOUND")
        with self._lock:
            original_binding = self._elements_by_ref.get(element_ref)
        if original_binding is None:
            raise ComputerError("STALE_ELEMENT_REFERENCE")
        original_fingerprint = self._descriptor_fingerprint(original_binding.descriptor)

        window_ref, before = self._resolve_element(element_ref)
        if action not in before.actions:
            raise ComputerError("ELEMENT_ACTION_UNSUPPORTED")
        if action == "set_value":
            if before.password: raise ComputerError("PROTECTED_CONTROL")
            if not isinstance(value, str) or len(value) > MAX_TEXT_INPUT: raise ComputerError("INVALID_TEXT_VALUE")
        # Re-observe immediately before a mutation.  A model-visible element_ref
        # is never sufficient authority by itself: the current semantic target
        # must still resolve to the same stable fingerprint.
        if action != "focus":
            try:
                _window_ref, current = self._resolve_element(element_ref)
            except ComputerError as error:
                if str(error) in {"STALE_ELEMENT_REFERENCE", "AMBIGUOUS_ELEMENT_REFERENCE"}:
                    raise ComputerError("TARGET_FINGERPRINT_CHANGED") from None
                raise
            if self._descriptor_fingerprint(self._element_descriptor(current)) != original_fingerprint:
                raise ComputerError("TARGET_FINGERPRINT_CHANGED")
            before = current
        try:
            executed = self._execute_ui_action(action, before, value)
        except ComputerError as error:
            if str(error) != "STALE_ELEMENT_REFERENCE":
                raise
            window_ref, before = self._resolve_element(element_ref)
            if action not in before.actions:
                raise ComputerError("ELEMENT_ACTION_UNSUPPORTED")
            executed = self._execute_ui_action(action, before, value)
        try:
            _same_window, after = self._resolve_element(element_ref)
        except ComputerError as error:
            if action == "invoke" and executed and str(error) == "STALE_ELEMENT_REFERENCE":
                return {"requested_action": "computer.ui.invoke", "target": {"window_ref": window_ref,
                        "element_ref": str(element_ref), "role": before.role},
                        "execution_status": "completed", "verification_status": "unverifiable",
                        "before": {"focused": before.focused, "checked": before.checked,
                                   "selected": before.selected}, "after": None, "error_category": None}
            raise
        verification = "unverifiable"
        verified = False
        if action == "focus": verified = after.focused
        elif action == "set_value" and not after.password: verified = after.value == value
        elif action == "toggle": verified = before.checked is not None and after.checked != before.checked
        elif action == "select": verified = after.selected is True
        if action != "invoke": verification = "verified" if executed and verified else "failed"
        return {"requested_action": f"computer.ui.{action}", "target": {"window_ref": window_ref,
                "element_ref": str(element_ref), "role": before.role,
                "target_fingerprint": original_fingerprint},
                "execution_status": "completed" if executed else "failed",
                "verification_status": verification if executed else "failed",
                "before": {"focused": before.focused, "checked": before.checked, "selected": before.selected},
                "after": {"focused": after.focused, "checked": after.checked, "selected": after.selected},
                "error_category": None if executed and verification != "failed" else
                    ("VERIFICATION_FAILED" if executed else "ACTION_FAILED")}
