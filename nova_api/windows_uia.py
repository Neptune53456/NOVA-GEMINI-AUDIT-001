"""Minimal Windows UI Automation adapter isolated from Nova's public model."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from typing import Any, Iterator

from .computer import ComputerError, NativeUIElement

_CONTROL_TYPES = {50000: "button", 50002: "checkbox", 50003: "combo_box", 50004: "edit",
    50005: "link", 50006: "image", 50007: "list_item", 50008: "list", 50009: "menu",
    50010: "menu_item", 50011: "progress", 50012: "radio", 50013: "scroll_bar",
    50014: "slider", 50015: "spinner", 50016: "status_bar", 50017: "tab",
    50018: "tab_item", 50019: "text", 50020: "toolbar", 50021: "tooltip",
    50023: "tree", 50024: "tree_item", 50025: "custom", 50030: "document", 50032: "window"}


class ComtypesUIAutomationBackend:
    """UIAutomationCore bridge with COM ownership scoped to each calling thread."""

    def __init__(self) -> None:
        pass

    @contextmanager
    def _session(self) -> Iterator[tuple[Any, Any]]:
        initialized = False
        try:
            import comtypes
            import comtypes.client
            # comtypes initializes its importing thread as STA by default. Balance a
            # matching initialization here instead of attempting to change apartments.
            comtypes.CoInitialize()
            initialized = True
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as uia
            automation = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
        except Exception:
            if initialized:
                comtypes.CoUninitialize()
            raise ComputerError("UI_AUTOMATION_INITIALIZATION_FAILED") from None
        try:
            yield uia, automation
        finally:
            automation = None
            comtypes.CoUninitialize()

    @staticmethod
    def _safe(element: Any, name: str, default: Any = None) -> Any:
        try: return getattr(element, name)
        except Exception: return default

    @staticmethod
    def _key(element: Any) -> tuple[int, ...]:
        return tuple(int(value) for value in element.GetRuntimeId())

    @staticmethod
    def _pattern(element: Any, pattern_id: int, interface: Any) -> Any | None:
        try:
            value = element.GetCurrentPattern(pattern_id)
            return value.QueryInterface(interface) if value else None
        except Exception:
            return None

    def _convert(self, element: Any, parent: object | None, uia: Any,
                 path: tuple[int, ...], window_id: int) -> NativeUIElement:
        key = (window_id, self._key(element))
        value_pattern = self._pattern(element, uia.UIA_ValuePatternId, uia.IUIAutomationValuePattern)
        toggle_pattern = self._pattern(element, uia.UIA_TogglePatternId, uia.IUIAutomationTogglePattern)
        selection_pattern = self._pattern(element, uia.UIA_SelectionItemPatternId, uia.IUIAutomationSelectionItemPattern)
        actions: list[str] = []
        if self._pattern(element, uia.UIA_InvokePatternId, uia.IUIAutomationInvokePattern): actions.append("invoke")
        if bool(self._safe(element, "CurrentIsKeyboardFocusable", False)): actions.append("focus")
        if value_pattern is not None: actions.append("set_value")
        if toggle_pattern is not None: actions.append("toggle")
        if selection_pattern is not None: actions.append("select")
        password = bool(self._safe(element, "CurrentIsPassword", False))
        raw_value = self._safe(value_pattern, "CurrentValue", "") if value_pattern is not None else None
        rectangle = self._safe(element, "CurrentBoundingRectangle")
        bounds = None
        try:
            bounds = (int(rectangle.left), int(rectangle.top),
                      int(rectangle.right), int(rectangle.bottom))
        except (AttributeError, TypeError, ValueError):
            pass
        return NativeUIElement(key,
            _CONTROL_TYPES.get(int(self._safe(element, "CurrentControlType", 0)), "unknown"),
            str(self._safe(element, "CurrentName", "") or ""), parent,
            bool(self._safe(element, "CurrentIsEnabled", False)),
            bool(self._safe(element, "CurrentIsOffscreen", True)),
            bool(self._safe(element, "CurrentHasKeyboardFocus", False)), value_pattern is not None,
            bool(self._safe(value_pattern, "CurrentIsReadOnly", True)) if value_pattern is not None else None,
            bool(self._safe(selection_pattern, "CurrentIsSelected", False)) if selection_pattern is not None else None,
            bool(self._safe(toggle_pattern, "CurrentToggleState", False)) if toggle_pattern is not None else None,
            password, None if password or value_pattern is None else str(raw_value or ""), tuple(actions),
            str(self._safe(element, "CurrentAutomationId", "") or ""), path, bounds)

    def _walk(self, root: Any, walker: Any, uia: Any, depth: int, limit: int,
              window_id: int) -> list[NativeUIElement]:
        queue: deque[tuple[Any, object | None, int, tuple[int, ...]]] = deque([(root, None, 0, ())])
        result: list[NativeUIElement] = []
        while queue and len(result) < limit:
            element, parent, level, path = queue.popleft()
            try: converted = self._convert(element, parent, uia, path, window_id)
            except Exception: continue
            result.append(converted)
            if level >= depth: continue
            try:
                child = walker.GetFirstChildElement(element)
                child_index = 0
                while child is not None:
                    queue.append((child, converted.native_key, level + 1, path + (child_index,)))
                    child_index += 1
                    child = walker.GetNextSiblingElement(child)
            except Exception:
                continue
        return result

    def elements(self, native_window_id: int, *, depth: int, max_elements: int) -> list[NativeUIElement]:
        try:
            with self._session() as (uia, automation):
                root = automation.ElementFromHandle(native_window_id)
                if root is None: raise ComputerError("WINDOW_NOT_FOUND")
                return self._walk(root, automation.ControlViewWalker, uia, depth, max_elements, native_window_id)
        except ComputerError: raise
        except Exception: raise ComputerError("UI_AUTOMATION_QUERY_FAILED") from None

    def _resolve(self, native_key: object, automation: Any) -> Any:
        if (not isinstance(native_key, tuple) or len(native_key) != 2 or
                not isinstance(native_key[0], int) or not isinstance(native_key[1], tuple)):
            raise ComputerError("STALE_ELEMENT_REFERENCE")
        window_id, runtime_id = native_key
        root = automation.ElementFromHandle(window_id)
        if root is None: raise ComputerError("STALE_ELEMENT_REFERENCE")
        walker, queue, visited = automation.ControlViewWalker, deque([(root, 0)]), 0
        while queue and visited < 200:
            element, level = queue.popleft(); visited += 1
            try:
                if self._key(element) == runtime_id: return element
            except Exception: continue
            if level >= 8: continue
            try:
                child = walker.GetFirstChildElement(element)
                while child is not None:
                    queue.append((child, level + 1)); child = walker.GetNextSiblingElement(child)
            except Exception: continue
        raise ComputerError("STALE_ELEMENT_REFERENCE")

    def _act(self, native_key: object, operation: Any) -> bool:
        try:
            with self._session() as (uia, automation):
                return bool(operation(self._resolve(native_key, automation), uia))
        except ComputerError: raise
        except Exception: raise ComputerError("UI_AUTOMATION_QUERY_FAILED") from None

    def invoke(self, native_key: object) -> bool:
        def op(element: Any, uia: Any) -> bool:
            pattern = self._pattern(element, uia.UIA_InvokePatternId, uia.IUIAutomationInvokePattern)
            if pattern is None: return False
            pattern.Invoke(); return True
        return self._act(native_key, op)

    def focus(self, native_key: object) -> bool:
        def op(element: Any, _uia: Any) -> bool: element.SetFocus(); return True
        return self._act(native_key, op)

    def set_value(self, native_key: object, value: str) -> bool:
        def op(element: Any, uia: Any) -> bool:
            pattern = self._pattern(element, uia.UIA_ValuePatternId, uia.IUIAutomationValuePattern)
            if pattern is None or bool(self._safe(pattern, "CurrentIsReadOnly", True)): return False
            pattern.SetValue(value); return True
        return self._act(native_key, op)

    def toggle(self, native_key: object) -> bool:
        def op(element: Any, uia: Any) -> bool:
            pattern = self._pattern(element, uia.UIA_TogglePatternId, uia.IUIAutomationTogglePattern)
            if pattern is None: return False
            pattern.Toggle(); return True
        return self._act(native_key, op)

    def select(self, native_key: object) -> bool:
        def op(element: Any, uia: Any) -> bool:
            pattern = self._pattern(element, uia.UIA_SelectionItemPatternId, uia.IUIAutomationSelectionItemPattern)
            if pattern is None: return False
            pattern.Select(); return True
        return self._act(native_key, op)
