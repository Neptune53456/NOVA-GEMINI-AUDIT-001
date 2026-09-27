"""Persistent, bounded mission orchestration over the existing capability registry."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Literal
from uuid import uuid4

from .agent_loop import ConfirmationRequest, ConfirmationStore, compact_observation
from .capabilities import CapabilityRegistry
from .durable_state import DurableConfirmationStore
from .execution_kernel import ExecutionKernel
from .journal import EventJournal
from .persistence import ensure_schema_version

MissionState = Literal["pending", "running", "awaiting_confirmation", "paused", "completed", "failed", "cancelled"]
MAX_MISSION_STEPS = 8
DEFAULT_MISSION_PATH = Path(__file__).resolve().parent.parent / ".runtime" / "nova_missions.sqlite3"


class MissionNotFound(KeyError):
    pass


class MissionStateError(RuntimeError):
    pass


class MissionPlanError(ValueError):
    """A compact, safe reason why a conversational mission cannot be planned."""


def plan_conversational_mission(content: str) -> list[dict[str, Any]] | None:
    """Recognise only an explicit, bounded mission request.

    This deliberately does not infer a capability from arbitrary prose.  The
    initial conversational surface has two useful, read-only plans; file and
    write operations remain available only through the existing AgentLoop and
    its confirmation flow.
    """
    normalized = " ".join(content.casefold().split())
    explicit = normalized.startswith(("mission:", "mission ", "planifie ", "planifier "))
    multi_step = any(marker in normalized for marker in ("plusieurs étapes", "plusieurs etapes", "en plusieurs", "suivi"))
    if not (explicit or multi_step):
        return None
    if not any(marker in normalized for marker in ("inspect", "analyse", "analy", "état", "etat", "projet", "git")):
        raise MissionPlanError("unsupported_mission_objective")
    return [
        {"capability_id": "project.basic_info", "arguments": {}},
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact_checkpoint(result: Any) -> dict[str, Any]:
    """Keep resumable metadata, never file bodies or full diffs."""
    observation = compact_observation(result.capability_id, result)
    observation.pop("content", None)
    observation.pop("diff_summary", None)
    return observation


@dataclass(frozen=True)
class Mission:
    mission_id: str
    conversation_id: str
    objective: str
    state: MissionState
    current_step: int
    step_count: int
    created_at: str
    updated_at: str
    last_error_category: str | None
    checkpoint: dict[str, Any]
    steps: list[dict[str, Any]]

    def public(self) -> dict[str, Any]:
        return {"mission_id": self.mission_id, "conversation_id": self.conversation_id,
                "objective": self.objective, "state": self.state, "current_step": self.current_step,
                "step_count": self.step_count, "created_at": self.created_at, "updated_at": self.updated_at,
                "last_error_category": self.last_error_category, "checkpoint": self.checkpoint,
                "steps": [{key: value for key, value in step.items() if key not in {"arguments", "confirmation_token"}}
                          for step in self.steps]}


class MissionStore:
    def __init__(self, path: str | Path = DEFAULT_MISSION_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as connection:
            ensure_schema_version(connection, expected=1, component="mission_store")
            connection.execute("""CREATE TABLE IF NOT EXISTS missions (
                mission_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, objective TEXT NOT NULL,
                state TEXT NOT NULL, current_step INTEGER NOT NULL, step_count INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_error_category TEXT,
                checkpoint_json TEXT NOT NULL, steps_json TEXT NOT NULL)""")
            # A request may have died after an external action but before its checkpoint.
            connection.execute("UPDATE missions SET state='paused', last_error_category='interrupted_uncertain', updated_at=? WHERE state='running'", (_now(),))
            # Confirmation HMACs are deliberately process-local and single-use; never revive one.
            connection.execute("UPDATE missions SET state='paused', last_error_category='confirmation_unavailable', updated_at=? WHERE state='awaiting_confirmation'", (_now(),))
            connection.execute("CREATE INDEX IF NOT EXISTS missions_conversation_idx ON missions(conversation_id, updated_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS missions_state_idx ON missions(state, updated_at DESC)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @staticmethod
    def _decode_json(value: object, *, expected: type) -> Any:
        try:
            decoded = json.loads(str(value))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise MissionStateError("corrupt_mission_state") from exc
        if not isinstance(decoded, expected):
            raise MissionStateError("corrupt_mission_state")
        return decoded

    @classmethod
    def _row(cls, row: sqlite3.Row) -> Mission:
        value = dict(row)
        checkpoint = cls._decode_json(value.get("checkpoint_json"), expected=dict)
        steps = cls._decode_json(value.get("steps_json"), expected=list)
        step_count = int(value["step_count"])
        current_step = int(value["current_step"])
        if len(steps) != step_count or not 0 <= current_step <= step_count or not all(isinstance(step, dict) for step in steps):
            raise MissionStateError("corrupt_mission_state")
        return Mission(mission_id=value["mission_id"], conversation_id=value["conversation_id"],
                       objective=value["objective"], state=value["state"], current_step=current_step,
                       step_count=step_count, created_at=value["created_at"], updated_at=value["updated_at"],
                       last_error_category=value["last_error_category"], checkpoint=checkpoint,
                       steps=steps)

    def create(self, conversation_id: str, objective: str, steps: list[dict[str, Any]]) -> Mission:
        now, mission_id = _now(), uuid4().hex
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO missions(
                    mission_id,conversation_id,objective,state,current_step,step_count,created_at,updated_at,
                    last_error_category,checkpoint_json,steps_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mission_id, conversation_id, objective, "pending", 0, len(steps), now, now, None,
                 json.dumps({"completed_steps": [], "last_result": None, "mutation_states": {}, "recovery_generation": 0}),
                 json.dumps(steps)),
            )
        return self.get(mission_id)

    def get(self, mission_id: str) -> Mission:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
        if row is None:
            raise MissionNotFound(mission_id)
        return self._row(row)

    def list(self, *, conversation_id: str | None = None, states: set[str] | None = None,
             limit: int = 100) -> list[Mission]:
        clauses: list[str] = []
        params: list[Any] = []
        if conversation_id is not None:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if states:
            bounded_states = tuple(sorted(str(state) for state in states if state))[:10]
            if bounded_states:
                clauses.append("state IN (" + ",".join("?" for _ in bounded_states) + ")")
                params.extend(bounded_states)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 100)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM missions{where} ORDER BY updated_at DESC LIMIT ?", tuple(params)
            ).fetchall()
        missions: list[Mission] = []
        for row in rows:
            try:
                missions.append(self._row(row))
            except MissionStateError:
                # One corrupted legacy row must not poison context/listing for every mission.
                continue
        return missions

    def update(self, mission: Mission) -> Mission:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("""UPDATE missions SET state=?, current_step=?, updated_at=?, last_error_category=?,
                checkpoint_json=?, steps_json=? WHERE mission_id=?""",
                (mission.state, mission.current_step, mission.updated_at, mission.last_error_category,
                 json.dumps(mission.checkpoint), json.dumps(mission.steps), mission.mission_id))
            if cursor.rowcount == 0:
                raise MissionNotFound(mission.mission_id)
        return mission


class MissionManager:
    """Executes pre-validated, small plans; it never grants capability authority."""
    def __init__(self, registry: CapabilityRegistry, journal: EventJournal, *, store: MissionStore | None = None,
                 confirmations: ConfirmationStore | None = None,
                 durable_confirmations: DurableConfirmationStore | None = None,
                 execution_kernel: ExecutionKernel | None = None) -> None:
        self.registry, self.journal = registry, journal
        self.store = store or MissionStore()
        self.confirmations = confirmations or ConfirmationStore()
        self.durable_confirmations = durable_confirmations or DurableConfirmationStore(
            self.store.path.with_name("nova_mission_confirmations.sqlite3")
        )
        self.execution = execution_kernel or ExecutionKernel(registry)
        self._cancelled: dict[str, Event] = {}
        self._pending: dict[str, tuple[str, ConfirmationRequest]] = {}
        self._pending_durable: dict[str, str] = {}
        self._lock = RLock()

    def create(self, conversation_id: str, objective: str, steps: list[dict[str, Any]]) -> Mission:
        if not isinstance(objective, str) or not objective.strip() or not isinstance(conversation_id, str):
            raise ValueError("invalid_mission")
        if not 1 <= len(steps) <= MAX_MISSION_STEPS:
            raise ValueError("invalid_step_count")
        normalized: list[dict[str, Any]] = []
        for step in steps:
            capability_id, arguments = step.get("capability_id"), step.get("arguments", {})
            if not isinstance(capability_id, str) or not isinstance(arguments, dict):
                raise ValueError("invalid_step")
            self.registry.validate_arguments(capability_id, arguments)
            normalized.append({"step_id": f"step-{len(normalized)+1}", "capability_id": capability_id, "arguments": arguments, "status": "pending"})
        mission = self.store.create(conversation_id, objective.strip(), normalized)
        self.journal.append("mission.created", mission_id=mission.mission_id, conversation_id=conversation_id, status="pending")
        return mission

    def run(self, mission_id: str, *, _continue: bool = False,
            notify: Callable[[str, Mission, str | None], None] | None = None) -> Mission:
        mission = self.store.get(mission_id)
        if mission.state not in ({"pending", "paused", "running"} if _continue else {"pending", "paused"}):
            raise MissionStateError("mission_not_resumable")
        if mission.last_error_category == "confirmation_unavailable":
            # Process-local HMACs never survive restart.  Remove stale public
            # confirmation data and issue a fresh request for the exact step.
            step = mission.steps[mission.current_step]
            step.pop("confirmation_token", None); step.pop("confirmation", None)
            step["status"] = "pending"
            mission = self._save(mission, state="paused", error=None)
        if mission.last_error_category == "interrupted_uncertain":
            mission = self._recover_interrupted(mission)
            if mission.state in {"failed", "cancelled", "completed"}:
                return mission
        self.journal.append("mission.started" if mission.state == "pending" else "mission.resumed",
                            mission_id=mission_id, conversation_id=mission.conversation_id, status="running")
        cancelled = self._cancelled.setdefault(mission_id, Event())
        mission = self._save(mission, state="running", error=None)
        if notify: notify("mission.started", mission, None)
        while mission.current_step < mission.step_count:
            if cancelled.is_set():
                mission = self._save(mission, state="cancelled", error=None)
                self.journal.append("mission.cancelled", mission_id=mission_id, status="cancelled")
                return mission
            index, step = mission.current_step, mission.steps[mission.current_step]
            capability_id, arguments = step["capability_id"], step["arguments"]
            step["status"] = "running"; mission = self._save(mission, state="running", error=None)
            self.journal.append("mission.step.started", mission_id=mission_id, conversation_id=mission.conversation_id,
                                capability_id=capability_id, status="running")
            if notify: notify("mission.step.started", mission, capability_id)
            if self.registry.lookup(capability_id).requires_confirmation:
                request = self.confirmations.create(capability_id, arguments, conversation_id=mission.conversation_id,
                    generation_id=mission_id, messages=[], model_turns=0, action_count=index)
                durable = self.durable_confirmations.create(
                    goal_id=mission_id, step_id=step["step_id"], capability_id=capability_id,
                    arguments=arguments, ttl_seconds=max(0.0, request.expires_at - __import__("time").monotonic()),
                )
                with self._lock:
                    self._pending[request.token] = (mission_id, request)
                    self._pending_durable[request.token] = durable.confirmation_request_id
                step["status"] = "awaiting_confirmation"; step["confirmation_token"] = request.token
                step["confirmation"] = request.public()
                checkpoint = dict(mission.checkpoint)
                checkpoint["outstanding_confirmation"] = {
                    "confirmation_request_id": durable.confirmation_request_id,
                    "step_id": step["step_id"],
                    "effect_fingerprint": durable.effect_fingerprint,
                }
                mission = self._save(mission, state="awaiting_confirmation", error=None, checkpoint=checkpoint)
                self.journal.append("mission.awaiting_confirmation", mission_id=mission_id, capability_id=capability_id,
                                    status="awaiting-confirmation")
                if notify: notify("mission.awaiting_confirmation", mission, capability_id)
                return mission
            mission = self._execute_step(mission, confirmed=False, notify=notify)
            if mission.state != "running": return mission
        mission = self._save(mission, state="completed", error=None)
        self.journal.append("mission.completed", mission_id=mission_id, status="completed")
        if notify: notify("mission.completed", mission, None)
        return mission

    def decide_confirmation(self, mission_id: str, token: str, approved: bool) -> Mission:
        with self._lock: pending = self._pending.get(token)
        if pending is None or pending[0] != mission_id: raise MissionStateError("invalid_confirmation")
        mission = self.store.get(mission_id)
        if mission.state != "awaiting_confirmation": raise MissionStateError("invalid_confirmation")
        with self._lock:
            durable_id = self._pending_durable.get(token)
        if durable_id is None:
            raise MissionStateError("invalid_confirmation")
        step = mission.steps[mission.current_step]
        try:
            self.durable_confirmations.decide(
                durable_id, approved=approved, capability_id=step["capability_id"], arguments=step["arguments"]
            )
            request = (self.confirmations.consume(token, conversation_id=mission.conversation_id)
                       if approved else self.confirmations.refuse(token, conversation_id=mission.conversation_id))
        except ValueError as error:
            raise MissionStateError(str(error) if str(error) == "confirmation_action_changed" else "invalid_confirmation") from error
        with self._lock:
            self._pending.pop(token, None); self._pending_durable.pop(token, None)
        if not approved:
            mission.steps[mission.current_step]["status"] = "paused"
            mission = self._save(mission, state="paused", error="confirmation_refused")
            self.journal.append("mission.paused", mission_id=mission_id, status="paused", error_category="confirmation_refused")
            return mission
        mission = self._save(mission, state="running", error=None)
        mission = self._execute_step(mission, confirmed=True)
        return self.run(mission_id, _continue=True) if mission.state == "running" else mission

    def cancel(self, mission_id: str) -> Mission:
        mission = self.store.get(mission_id)
        if mission.state in {"completed", "failed", "cancelled"}: return mission
        self._cancelled.setdefault(mission_id, Event()).set()
        if mission.state != "running":
            mission = self._save(mission, state="cancelled", error=None)
            self.journal.append("mission.cancelled", mission_id=mission_id, status="cancelled")
        return mission

    def _execute_step(self, mission: Mission, *, confirmed: bool,
                      notify: Callable[[str, Mission, str | None], None] | None = None) -> Mission:
        step, capability_id = mission.steps[mission.current_step], mission.steps[mission.current_step]["capability_id"]
        capability = self.registry.lookup(capability_id)
        if capability.requires_confirmation or capability_id == "filesystem.write":
            mission = self._mark_mutation(mission, step, "STARTED_UNCERTAIN")
        result = self.registry.execute(capability_id, step["arguments"], confirmed=confirmed)
        if result.status != "success" or not result.verified:
            step["status"] = "failed"
            state = "paused" if result.error_category == "repeated_action" else "failed"
            mission = self._save(mission, state=state, error=result.error_category or "action_failed")
            self.journal.append("mission.paused" if state == "paused" else "mission.failed", mission_id=mission.mission_id,
                                action_id=result.action_id, capability_id=capability_id, status=state,
                                error_category=result.error_category)
            if notify: notify("mission.paused" if state == "paused" else "mission.failed", mission, capability_id)
            return mission
        step["status"] = "completed"; step.pop("confirmation_token", None); step.pop("confirmation", None)
        checkpoint = dict(mission.checkpoint); checkpoint["last_result"] = _compact_checkpoint(result)
        checkpoint["transaction_id"] = (result.result or {}).get("transaction_id")
        checkpoint["completed_steps"] = [*checkpoint.get("completed_steps", []), mission.current_step]
        checkpoint["outstanding_confirmation"] = None
        if capability.requires_confirmation or capability_id == "filesystem.write":
            mission = self._mark_mutation(
                mission, step, "VERIFIED", action_id=result.action_id,
                transaction_id=(result.result or {}).get("transaction_id"),
            )
            checkpoint = dict(mission.checkpoint)
            checkpoint["last_result"] = _compact_checkpoint(result)
            checkpoint["transaction_id"] = (result.result or {}).get("transaction_id")
            checkpoint["completed_steps"] = [*checkpoint.get("completed_steps", []), mission.current_step]
            checkpoint["outstanding_confirmation"] = None
        mission = self._save(mission, state="running", current_step=mission.current_step + 1, checkpoint=checkpoint, error=None)
        self.journal.append("mission.step.completed", mission_id=mission.mission_id, action_id=result.action_id,
                            transaction_id=checkpoint["transaction_id"], capability_id=capability_id, status="completed")
        self.journal.append("mission.checkpoint", mission_id=mission.mission_id, transaction_id=checkpoint["transaction_id"], status="saved")
        if notify: notify("mission.step.completed", mission, capability_id)
        return mission

    def _mark_mutation(self, mission: Mission, step: dict[str, Any], state: str, **metadata: Any) -> Mission:
        checkpoint = dict(mission.checkpoint)
        states = dict(checkpoint.get("mutation_states", {}))
        entry = self.execution.mutation_entry(
            mission.mission_id, step["step_id"], step["capability_id"], step["arguments"], state, **metadata
        )
        states[entry["mutation_id"]] = entry
        checkpoint["mutation_states"] = states
        checkpoint["mutation_sequence"] = max(int(checkpoint.get("mutation_sequence", 0)), len(states))
        return self._save(mission, state=mission.state, error=mission.last_error_category, checkpoint=checkpoint)

    def _recover_interrupted(self, mission: Mission) -> Mission:
        if mission.current_step >= mission.step_count:
            return self._save(mission, state="completed", error=None)
        step = mission.steps[mission.current_step]
        mutation_id = self.execution.mutation_entry(
            mission.mission_id, step["step_id"], step["capability_id"], step["arguments"], "NOT_STARTED"
        )["mutation_id"]
        entry = (mission.checkpoint.get("mutation_states", {}) or {}).get(mutation_id)
        if not entry or entry.get("state") != "STARTED_UNCERTAIN":
            # Read-only or not-started steps are safe to re-observe.
            checkpoint = dict(mission.checkpoint)
            checkpoint["recovery_generation"] = int(checkpoint.get("recovery_generation", 0)) + 1
            return self._save(mission, state="paused", error=None, checkpoint=checkpoint)
        decision = self.execution.reconcile(step["capability_id"], step["arguments"])
        if decision.state == "completed":
            mission = self._mark_mutation(mission, step, "VERIFIED",
                                          transaction_id=decision.transaction_id, recovered_after_restart=True)
            checkpoint = dict(mission.checkpoint)
            completed = list(checkpoint.get("completed_steps", []))
            if mission.current_step not in completed: completed.append(mission.current_step)
            checkpoint["completed_steps"] = completed
            checkpoint["transaction_id"] = decision.transaction_id
            checkpoint["recovery_generation"] = int(checkpoint.get("recovery_generation", 0)) + 1
            step["status"] = "completed"
            self.journal.append("mission.mutation.recovered", mission_id=mission.mission_id,
                                transaction_id=decision.transaction_id, capability_id=step["capability_id"], status="verified")
            return self._save(mission, state="paused", error=None, current_step=mission.current_step + 1, checkpoint=checkpoint)
        if decision.state == "not_applied":
            mission = self._mark_mutation(mission, step, "NOT_STARTED", recovered_after_restart=True)
            checkpoint = dict(mission.checkpoint)
            checkpoint["recovery_generation"] = int(checkpoint.get("recovery_generation", 0)) + 1
            return self._save(mission, state="paused", error=None, checkpoint=checkpoint)
        return self._save(mission, state="paused", error="uncertain_mutation_requires_verification")

    def _save(self, mission: Mission, *, state: MissionState, error: str | None,
              current_step: int | None = None, checkpoint: dict[str, Any] | None = None) -> Mission:
        return self.store.update(Mission(mission.mission_id, mission.conversation_id, mission.objective, state,
            mission.current_step if current_step is None else current_step, mission.step_count, mission.created_at, _now(),
            error, mission.checkpoint if checkpoint is None else checkpoint, mission.steps))
