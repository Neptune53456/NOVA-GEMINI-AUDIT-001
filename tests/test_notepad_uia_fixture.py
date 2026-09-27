from __future__ import annotations

import pytest

from nova_api.computer import NativeWindow
from scripts.notepad_uia_fixture import (FixtureWindow, NotExecutable, close_owned_fixture,
                                         prove_fixture_window, select_editor)


def window(hwnd: int, pid: int, application: str = "Notepad.exe") -> NativeWindow:
    return NativeWindow(hwnd, pid, application, "Untitled - Notepad", "normal")


def test_fixture_ownership_accepts_new_process_window_despite_broker_pid() -> None:
    fixture = prove_fixture_window([window(1, 10)], [window(1, 10), window(2, 30)],
                                   launched_process_id=20, launched_at=100.0,
                                   process_created_at={10: 50.0, 30: 100.1})
    assert fixture.hwnd == 2
    assert fixture.ownership_proof == "window_diff_and_creation_time_broker_delegation"


def test_fixture_ownership_accepts_unique_new_window_in_broker_process() -> None:
    fixture = prove_fixture_window([window(1, 10)], [window(1, 10), window(2, 10)],
                                   launched_process_id=20, launched_at=100.0,
                                   process_created_at={10: 50.0})
    assert fixture.hwnd == 2
    assert fixture.shared_process is True
    assert fixture.ownership_proof == "unique_window_diff_with_launch_timing_broker_delegation"


@pytest.mark.parametrize("current,creation_times", [
    ([window(1, 10), window(2, 20), window(3, 30)], {20: 100.1, 30: 100.2}),
    ([window(1, 10), window(2, 10)], {}),
    ([window(1, 10), window(2, 20)], {20: 50.0}),
])
def test_fixture_ownership_fails_closed_when_evidence_is_ambiguous(current, creation_times) -> None:
    with pytest.raises(NotExecutable):
        prove_fixture_window([window(1, 10)], current, launched_process_id=20,
                             launched_at=100.0, process_created_at=creation_times)


def test_editor_selection_requires_one_writable_semantic_editor() -> None:
    editor = {"element_ref": "element_editor", "role": "edit", "enabled": True,
              "visible": True, "editable": True, "read_only": False,
              "supported_actions": ["focus", "set_value"]}
    assert select_editor({"elements": [editor, {"role": "toolbar"}]}) is editor
    with pytest.raises(NotExecutable, match="editable_candidate_count_2"):
        select_editor({"elements": [editor, dict(editor, element_ref="element_other")]})


def fixture(*, hwnd: int = 2, pid: int = 20, created_at: float = 100.0,
            bounds=None) -> FixtureWindow:
    return FixtureWindow(hwnd, pid, created_at, 99, "test-proof", True, bounds)


def cleanup(value: FixtureWindow, current: list[NativeWindow], *, created_at=None,
            request_close=lambda _hwnd: True, window_exists=lambda _hwnd: False,
            timeout=1.0, monotonic=lambda: 0.0, sleep=lambda _seconds: None) -> str:
    creation_times = {value.process_id: value.process_created_at}
    if created_at is not None:
        creation_times[value.process_id] = created_at
    return close_owned_fixture(value, current, process_created_at=creation_times,
                               request_close=request_close, window_exists=window_exists,
                               timeout=timeout, monotonic=monotonic, sleep=sleep)


def test_cleanup_requests_close_for_proven_fixture_hwnd_only() -> None:
    requested = []
    value = fixture()
    result = cleanup(value, [window(2, 20)], request_close=lambda hwnd: requested.append(hwnd) or True)
    assert result == "closed_proven_fixture_window"
    assert requested == [2]


def test_cleanup_does_not_touch_preexisting_window() -> None:
    requested = []
    result = cleanup(fixture(), [window(1, 10)],
                     request_close=lambda hwnd: requested.append(hwnd) or True)
    assert result == "left_open_ambiguous_window_identity"
    assert requested == []


def test_cleanup_rejects_reused_hwnd_with_changed_identity() -> None:
    requested = []
    result = cleanup(fixture(), [window(2, 30)],
                     request_close=lambda hwnd: requested.append(hwnd) or True)
    assert result == "left_open_window_identity_changed"
    assert requested == []


def test_cleanup_rejects_ambiguous_window_snapshot() -> None:
    requested = []
    result = cleanup(fixture(), [window(2, 20), window(2, 20)],
                     request_close=lambda hwnd: requested.append(hwnd) or True)
    assert result == "left_open_ambiguous_window_identity"
    assert requested == []


def test_cleanup_targets_only_fixture_hwnd_in_shared_notepad_process() -> None:
    requested = []
    value = fixture()
    result = cleanup(value, [window(1, 20), window(2, 20), window(3, 20)],
                     request_close=lambda hwnd: requested.append(hwnd) or True)
    assert result == "closed_proven_fixture_window"
    assert requested == [2]


def test_cleanup_timeout_reports_leftover_without_process_kill() -> None:
    requested = []
    ticks = iter([0.0, 0.0, 0.5, 1.0])
    result = cleanup(fixture(), [window(2, 20)],
                     request_close=lambda hwnd: requested.append(hwnd) or True,
                     window_exists=lambda _hwnd: True, monotonic=lambda: next(ticks))
    assert result == "left_open_bounded_cleanup_timeout"
    assert requested == [2]
