from pathlib import Path

import pytest

from nova_api.capabilities import build_default_registry
from nova_api.journal import EventJournal
from nova_api.missions import MissionManager, MissionStateError, MissionStore


def manager(tmp_path: Path) -> MissionManager:
    journal = EventJournal(tmp_path / "events.sqlite3")
    return MissionManager(build_default_registry(journal, project_root=tmp_path), journal,
                          store=MissionStore(tmp_path / "missions.sqlite3"))


def test_restart_reissues_confirmation_instead_of_reusing_token(tmp_path: Path) -> None:
    first = manager(tmp_path)
    mission = first.create("c", "write", [
        {"capability_id": "filesystem.write", "arguments": {"path": "x.txt", "content": "hello"}},
    ])
    waiting = first.run(mission.mission_id)
    old_token = waiting.steps[0]["confirmation_token"]

    restarted = manager(tmp_path)
    paused = restarted.store.get(mission.mission_id)
    assert paused.state == "paused"
    assert paused.last_error_category == "confirmation_unavailable"
    fresh = restarted.run(mission.mission_id)
    assert fresh.state == "awaiting_confirmation"
    assert fresh.steps[0]["confirmation_token"] != old_token
    records = restarted.durable_confirmations.for_goal(mission.mission_id)
    assert records[-2].decision == "expired"
    assert records[-1].decision == "pending"


def test_confirmation_is_bound_to_exact_mission_effect(tmp_path: Path) -> None:
    service = manager(tmp_path)
    mission = service.create("c", "write", [
        {"capability_id": "filesystem.write", "arguments": {"path": "x.txt", "content": "approved"}},
    ])
    waiting = service.run(mission.mission_id)
    token = waiting.steps[0]["confirmation_token"]
    changed = service.store.get(mission.mission_id)
    changed.steps[0]["arguments"]["content"] = "changed"
    service.store.update(changed)
    with pytest.raises(MissionStateError, match="confirmation_action_changed"):
        service.decide_confirmation(mission.mission_id, token, True)
    assert not (tmp_path / "x.txt").exists()


def test_completed_crash_window_mission_write_is_recovered_without_replay(tmp_path: Path) -> None:
    service = manager(tmp_path)
    mission = service.create("c", "write", [
        {"capability_id": "filesystem.write", "arguments": {"path": "x.txt", "content": "once"}},
    ])
    step = mission.steps[0]
    mission = service._mark_mutation(mission, step, "STARTED_UNCERTAIN")
    service._save(mission, state="running", error=None)
    result = service.registry.execute("filesystem.write", step["arguments"], confirmed=True)
    txid = result.result["transaction_id"]

    restarted = manager(tmp_path)
    recovered = restarted.run(mission.mission_id)
    assert recovered.state == "completed"
    assert recovered.current_step == 1
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "once"
    assert recovered.checkpoint["transaction_id"] == txid
    entries = list(recovered.checkpoint["mutation_states"].values())
    assert entries[0]["state"] == "VERIFIED"
    assert entries[0]["recovered_after_restart"] is True


def test_read_only_interrupted_mission_resumes_by_reobserving(tmp_path: Path) -> None:
    service = manager(tmp_path)
    mission = service.create("c", "inspect", [
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
    ])
    with service.store._connect() as db:
        db.execute("UPDATE missions SET state='running' WHERE mission_id=?", (mission.mission_id,))
    restarted = manager(tmp_path)
    completed = restarted.run(mission.mission_id)
    assert completed.state == "completed"
    assert completed.checkpoint["recovery_generation"] == 1
