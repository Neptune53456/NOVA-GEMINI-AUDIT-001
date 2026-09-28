"""Bounded goal pursuit above individual AgentLoop turns and durable missions.

The runner deliberately accepts an already bounded plan.  A model-backed planner may
produce that plan, but execution, verification, budgets, recovery and persistence are
deterministic here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock
from time import monotonic
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4

from .agent_loop import ConfirmationRequest, ConfirmationStore, compact_observation
from .durable_state import DurableConfirmationStore, action_fingerprint
from .risk import RiskEngine
from .application_memory import ApplicationMemory
from .execution_kernel import ExecutionKernel, stable_mutation_id
from .capabilities import CapabilityRegistry, ConfirmationRequired
from .context_builder import ContextBuilder
from .journal import EventJournal
from .initial_goal_planner import InitialGoalPlanner, InitialPlanInvalid, PlanStep
from .memory_store import MemoryRejected, MemoryStore
from .outcome_learning import ExecutionOutcome, OutcomeLearner
from .persistence import ensure_schema_version
from .uncertainty import UncertaintyEngine
from .recovery import RecoveryPolicy
from .deliberation import DeliberationEngine
from .missions import MAX_MISSION_STEPS
from .transactions import TransactionError
from .workspace import WorkspacePathError

GoalStatus = Literal[
    "pending", "running", "awaiting_confirmation", "paused", "completed_verified",
    "completed_unverified", "blocked", "failed", "cancelled",
]
GoalPhase = Literal["understand", "plan", "execute", "verify", "decide", "terminal"]
DEFAULT_GOAL_PATH = Path(__file__).resolve().parent.parent / ".runtime" / "nova_goals.sqlite3"
TRANSIENT_ARGUMENTS = frozenset({"window_ref", "element_ref", "image_ref"})


@dataclass(frozen=True)
class GoalBudgets:
    max_steps: int = MAX_MISSION_STEPS
    max_replans: int = 2
    max_failed_steps: int = 3
    max_mutating_actions: int = 4
    max_discovery_actions: int = 12
    max_model_calls: int = 4
    max_duration_seconds: int = 900


@dataclass(frozen=True)
class GoalExecution:
    goal_id: str
    mission_id: str | None
    conversation_id: str
    objective: str
    success_criteria: str
    status: GoalStatus
    phase: GoalPhase
    plan_version: int
    current_step: int
    replans: int
    failed_steps: int
    mutating_actions: int
    discovery_actions: int
    model_calls: int
    created_at: str
    updated_at: str
    plan: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    blockers: list[str]
    checkpoint: dict[str, Any]
    metrics: dict[str, Any]

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        for step in value["plan"]:
            step.pop("arguments", None)
            step.pop("confirmation_token", None)
        return value


class GoalNotFound(KeyError):
    pass


class GoalStateError(RuntimeError):
    pass


class GoalPlanner(Protocol):
    def replan(self, objective: str, context: str, state: GoalExecution) -> list[dict[str, Any]]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value).split())
    return text[:limit]


class GoalStore:
    """Compact goal/checkpoint store; MissionStore remains authority for missions."""

    def __init__(self, path: str | Path = DEFAULT_GOAL_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as db:
            ensure_schema_version(db, expected=1, component="goal_store")
            db.execute("""CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)""")
            rows = db.execute("SELECT goal_id, payload_json FROM goals").fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                    if not isinstance(payload, dict):
                        continue
                except (json.JSONDecodeError, TypeError, ValueError):
                    # A damaged legacy row must not prevent Nova from starting.
                    # It remains untouched for diagnosis and fails closed on direct access.
                    continue
                status = payload.get("status")
                if status == "running":
                    payload["status"], payload["phase"] = "paused", "decide"
                    payload.setdefault("blockers", []).append("interrupted_reobserve_required")
                elif status == "awaiting_confirmation":
                    # HMAC confirmation tokens are intentionally process-local.
                    # Restart invalidates the old grant and forces a fresh card.
                    payload["status"], payload["phase"] = "paused", "execute"
                    payload.setdefault("blockers", []).append("confirmation_restart_required")
                    current = int(payload.get("current_step", 0))
                    plan = payload.get("plan") or []
                    if 0 <= current < len(plan) and isinstance(plan[current], dict):
                        plan[current]["status"] = "pending"
                        plan[current].pop("confirmation_token", None)
                        plan[current].pop("confirmation", None)
                else:
                    continue
                payload["updated_at"] = _now()
                db.execute("UPDATE goals SET payload_json=? WHERE goal_id=?",
                           (json.dumps(payload), row["goal_id"]))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    @staticmethod
    def _decode(payload: str) -> GoalExecution:
        try:
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("goal payload must be an object")
            return GoalExecution(**value)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise GoalStateError("corrupt_goal_state") from exc

    def save(self, goal: GoalExecution) -> GoalExecution:
        payload = json.dumps(asdict(goal), ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO goals VALUES (?, ?) ON CONFLICT(goal_id) DO UPDATE SET payload_json=excluded.payload_json",
                       (goal.goal_id, payload))
        return goal

    def get(self, goal_id: str) -> GoalExecution:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT payload_json FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
        if row is None:
            raise GoalNotFound(goal_id)
        return self._decode(row["payload_json"])

    def list(self, *, conversation_id: str | None = None) -> list[GoalExecution]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT payload_json FROM goals").fetchall()
        goals: list[GoalExecution] = []
        for row in rows:
            try:
                goals.append(self._decode(row["payload_json"]))
            except GoalStateError:
                continue
        if conversation_id is not None:
            goals = [goal for goal in goals if goal.conversation_id == conversation_id]
        return sorted(goals, key=lambda goal: goal.updated_at, reverse=True)


class GoalRunner:
    def __init__(self, registry: CapabilityRegistry, journal: EventJournal, context: ContextBuilder,
                 memory: MemoryStore, *, store: GoalStore | None = None,
                 confirmations: ConfirmationStore | None = None, planner: GoalPlanner | None = None,
                 initial_planner: InitialGoalPlanner | None = None,
                 durable_confirmations: DurableConfirmationStore | None = None,
                 risk_engine: RiskEngine | None = None,
                 execution_kernel: ExecutionKernel | None = None,
                 uncertainty_engine: UncertaintyEngine | None = None,
                 outcome_learner: OutcomeLearner | None = None,
                 recovery_policy: RecoveryPolicy | None = None,
                 deliberation_engine: DeliberationEngine | None = None,
                 budgets: GoalBudgets = GoalBudgets()) -> None:
        self.registry, self.journal, self.context, self.memory = registry, journal, context, memory
        self.store, self.confirmations, self.planner, self.budgets = (
            store or GoalStore(), confirmations or ConfirmationStore(), planner, budgets)
        self._pending: dict[str, ConfirmationRequest] = {}
        self._pending_durable: dict[str, str] = {}
        durable_path = self.store.path.with_name("nova_confirmations.sqlite3")
        self.durable_confirmations = durable_confirmations or DurableConfirmationStore(durable_path)
        self.risk_engine = risk_engine or RiskEngine()
        self.execution = execution_kernel or ExecutionKernel(registry)
        self.uncertainty = uncertainty_engine or UncertaintyEngine()
        self.outcomes = outcome_learner or OutcomeLearner(memory)
        self.recovery_policy = recovery_policy or RecoveryPolicy()
        self.deliberation = deliberation_engine
        self.application_memory = getattr(registry, "application_memory", None)
        self._started: dict[str, float] = {}
        self._cancelled: dict[str, Event] = {}
        self.initial_planner = initial_planner

    def create_from_objective(self, conversation_id: str, objective: str,
                              *, mission_id: str | None = None) -> GoalExecution:
        if self.initial_planner is None:
            raise InitialPlanInvalid("initial_planner_unavailable")
        self.journal.append("goal.intent.detected", conversation_id=conversation_id,
                            mission_id=mission_id, status="detected")
        self.journal.append("goal.planning.started", conversation_id=conversation_id,
                            mission_id=mission_id, status="planning")
        package = self.context.build(objective, conversation_id=conversation_id)
        try:
            planned = self.initial_planner.plan(
                objective, package, max_model_calls=self.budgets.max_model_calls,
                conversation_id=conversation_id, mission_id=mission_id)
            try:
                return self.create(conversation_id, objective, planned.success_criteria,
                                   planned.semantic_steps(),
                                   mission_id=mission_id, planning_calls=planned.model_calls)
            except (ValueError, WorkspacePathError):
                raise InitialPlanInvalid(
                    "BACKEND_STEP_COMPILATION_FAILED", ("steps",),
                    stage="runtime_goal_step_compilation", public_code="PLAN_INVALID",
                ) from None
        except InitialPlanInvalid as error:
            self.journal.append("goal.plan.rejected", conversation_id=conversation_id,
                                mission_id=mission_id, status="blocked",
                                error_category=(error.reason_code if error.reason_code != "PLAN_INVALID"
                                                else "INVALID_PLAN_SCHEMA"),
                                structural_fingerprint=(error.structural_fingerprint
                                                        or self.initial_planner.last_structural_fingerprint))
            raise

    def resumable(self, conversation_id: str) -> list[GoalExecution]:
        statuses = {"pending", "running", "paused", "awaiting_confirmation"}
        same_conversation = [goal for goal in self.store.list(conversation_id=conversation_id)
                             if goal.status in statuses]
        if same_conversation:
            return same_conversation
        return [goal for goal in self.store.list() if goal.status in statuses]

    def create(self, conversation_id: str, objective: str, success_criteria: str,
               plan: list[dict[str, Any]], *, mission_id: str | None = None,
               planning_calls: int = 0) -> GoalExecution:
        if not objective.strip() or not success_criteria.strip() or not 1 <= len(plan) <= self.budgets.max_steps:
            raise ValueError("invalid_goal")
        if mission_id is not None:
            if self.context.missions is None:
                raise ValueError("mission_store_unavailable")
            self.context.missions.store.get(mission_id)
        normalized = self._validate_plan(plan)
        required_readback = None
        for step in normalized:
            if step["capability_id"] != "filesystem.read":
                continue
            path = step["arguments"].get("path")
            content = step["verification"].get("content")
            if isinstance(path, str) and isinstance(content, str) and success_criteria == (
                    f"{path} exists in the allowed workspace and its content equals {content}"):
                required_readback = {"path": path, "content": content}
                break
        now = _now()
        goal = GoalExecution(uuid4().hex, mission_id, conversation_id, objective.strip(), success_criteria.strip(),
            "pending", "understand", 1, 0, 0, 0, 0, 0, planning_calls, now, now, normalized, [], [],
            {"completed_step_ids": [], "plan_version": 1, "next_phase": "execute",
             "required_readback": required_readback},
            {"planning_calls": planning_calls, "model_turns": 0, "elapsed_ms": 0})
        self.store.save(goal)
        self.journal.append("goal.started", goal_id=goal.goal_id, mission_id=mission_id, conversation_id=conversation_id, status="pending")
        self.journal.append("goal.plan.created", goal_id=goal.goal_id, mission_id=mission_id, conversation_id=conversation_id, status="created")
        return goal

    def _validate_plan(self, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for index, raw in enumerate(plan):
            semantic = PlanStep.from_runtime(raw)
            capability_id, arguments = semantic.capability_id, semantic.arguments
            self.registry.validate_arguments(capability_id, arguments)
            capability = self.registry.lookup(capability_id)
            risk = self.risk_engine.assess(
                capability, arguments,
                context=self.registry.risk_context(capability_id, arguments),
            )
            result.append({"step_id": str(raw.get("step_id") or f"step-{index + 1}"),
                "objective": semantic.objective,
                "expected_evidence": semantic.expected_evidence,
                "capability_id": capability_id, "arguments": arguments,
                "verification": semantic.verification,
                "status": "pending",
                "risk": risk.level,
                "risk_reasons": list(risk.reasons),
                "reversible": capability.reversible})
        return result

    @staticmethod
    def _mutation_id(goal: GoalExecution, step: dict[str, Any]) -> str:
        return stable_mutation_id(
            f"{goal.goal_id}:v{goal.plan_version}", step["step_id"],
            step["capability_id"], step.get("arguments", {}),
        )

    def _mark_mutation_state(self, goal: GoalExecution, step: dict[str, Any], state: str,
                             **metadata: Any) -> GoalExecution:
        checkpoint = dict(goal.checkpoint)
        states = dict(checkpoint.get("mutation_states", {}))
        mutation_id = self._mutation_id(goal, step)
        entry = self.execution.mutation_entry(
            f"{goal.goal_id}:v{goal.plan_version}", step["step_id"], step["capability_id"],
            step.get("arguments", {}), state, **metadata,
        )
        # Keep the stable key used by the goal checkpoint contract.
        states[mutation_id] = {**entry, "mutation_id": mutation_id}
        checkpoint["mutation_states"] = states
        checkpoint["mutation_sequence"] = max(int(checkpoint.get("mutation_sequence", 0)), len(states))
        return self._save(goal, checkpoint=checkpoint)

    def _recover_uncertain_mutation(self, goal: GoalExecution) -> tuple[GoalExecution, bool]:
        """Re-observe a filesystem write after restart; never replay blindly."""
        if goal.current_step >= len(goal.plan):
            return goal, False
        step = goal.plan[goal.current_step]
        mutation_id = self._mutation_id(goal, step)
        state = (goal.checkpoint.get("mutation_states", {}).get(mutation_id) or {}).get("state")
        if state != "STARTED_UNCERTAIN":
            return goal, False
        arguments = step.get("arguments", {})
        content = arguments.get("content")
        path = arguments.get("path")
        if not isinstance(content, str) or not isinstance(path, str):
            return self._terminal(goal, "blocked", "uncertain_mutation_invalid_arguments"), True
        decision = self.execution.reconcile(step["capability_id"], arguments)
        reconciliation, transaction_id = decision.state, decision.transaction_id
        if reconciliation == "unsupported":
            return self._terminal(goal, "blocked", "uncertain_mutation_requires_manual_recovery"), True
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if reconciliation == "completed":
            resolved = self.registry.transactions.workspace.resolve(path)
            if not resolved.is_file() or hashlib.sha256(resolved.read_bytes()).hexdigest() != expected_hash:
                return self._terminal(goal, "blocked", "uncertain_mutation_verification_failed"), True
            evidence = {
                "evidence_id": uuid4().hex, "source": "DETERMINISTIC", "provenance": "DETERMINISTIC",
                "step_id": step["step_id"], "summary": "Recovered exact filesystem write by hash after restart",
                "verification_state": "VERIFIED", "timestamp": _now(),
                "capability_id": step["capability_id"], "action_id": f"recovered:{transaction_id}",
            }
            step["status"] = "completed"
            step.pop("confirmation_token", None); step.pop("confirmation", None)
            goal = self._mark_mutation_state(goal, step, "VERIFIED", transaction_id=transaction_id,
                                             recovered_after_restart=True)
            checkpoint = dict(goal.checkpoint)
            completed = list(checkpoint.get("completed_step_ids", []))
            if step["step_id"] not in completed:
                completed.append(step["step_id"])
            checkpoint.update({"completed_step_ids": completed, "last_evidence_id": evidence["evidence_id"],
                               "next_phase": "execute", "outstanding_confirmation": None})
            goal = self._save(goal, current_step=goal.current_step + 1, evidence=[*goal.evidence, evidence],
                              checkpoint=checkpoint, phase="decide", blockers=[])
            self.journal.append("goal.mutation.recovered", goal_id=goal.goal_id, mission_id=goal.mission_id,
                                transaction_id=transaction_id, capability_id=step["capability_id"], status="verified")
            return goal, True
        if reconciliation in {"none", "not_applied"}:
            goal = self._mark_mutation_state(goal, step, "NOT_STARTED", recovery=reconciliation)
            return self._save(goal, blockers=[]), False
        return self._terminal(goal, "blocked", "uncertain_mutation_conflict"), True

    def run(self, goal_id: str, *, confirmed_token: str | None = None, approved: bool = True) -> GoalExecution:
        goal = self.store.get(goal_id)
        if goal.status not in {"pending", "paused", "running", "awaiting_confirmation"}:
            raise GoalStateError("goal_not_resumable")
        if goal.status == "awaiting_confirmation":
            if not confirmed_token:
                return goal
            request = self._pending.pop(confirmed_token, None)
            durable_id = self._pending_durable.pop(confirmed_token, None)
            if request is None or durable_id is None:
                raise GoalStateError("confirmation_unavailable")
            current_step = goal.plan[goal.current_step]
            if action_fingerprint(request.capability_id, request.arguments) != action_fingerprint(
                    current_step["capability_id"], current_step["arguments"]):
                raise GoalStateError("confirmation_action_changed")
            try:
                self.durable_confirmations.decide(
                    durable_id, approved=approved, capability_id=current_step["capability_id"],
                    arguments=current_step["arguments"],
                )
            except ValueError as error:
                raise GoalStateError(str(error)) from error
            if not approved:
                try:
                    self.confirmations.refuse(confirmed_token, conversation_id=goal.conversation_id)
                except ValueError as error:
                    raise GoalStateError("invalid_confirmation") from error
                goal.plan[goal.current_step]["status"] = "paused"
                goal = self._save(goal, status="paused", phase="decide", blockers=["confirmation_refused"])
                self.journal.append("goal.paused", goal_id=goal.goal_id, mission_id=goal.mission_id,
                                    status="paused", error_category="confirmation_refused")
                return goal
            try:
                self.confirmations.consume(confirmed_token, conversation_id=goal.conversation_id)
            except ValueError as error:
                raise GoalStateError("invalid_confirmation") from error
        else:
            request = None
        if "interrupted_reobserve_required" in goal.blockers:
            goal, recovered = self._recover_uncertain_mutation(goal)
            if goal.status == "blocked":
                return goal
            if recovered and goal.current_step >= len(goal.plan):
                current_ids = {step["step_id"] for step in goal.plan}
                verified_ids = {item["step_id"] for item in goal.evidence if item["verification_state"] == "VERIFIED"}
                status = "completed_verified" if current_ids <= verified_ids else "completed_unverified"
                return self._terminal(goal, status, None)
            goal = self._refresh_after_restart(goal)
            if goal.status == "blocked":
                return goal
        self._started.setdefault(goal_id, monotonic())
        cancelled = self._cancelled.setdefault(goal_id, Event())
        goal = self._save(goal, status="running", phase="execute", blockers=[])
        self.journal.append("goal.resumed" if goal.current_step else "goal.started",
                            goal_id=goal.goal_id, mission_id=goal.mission_id, conversation_id=goal.conversation_id, status="running")
        while goal.current_step < len(goal.plan):
            if cancelled.is_set():
                return self._terminal(goal, "cancelled", "cancel_requested")
            if self._budget_exhausted(goal):
                return self._terminal(goal, "blocked", "GOAL_BUDGET_EXCEEDED")
            step = goal.plan[goal.current_step]
            capability = self.registry.lookup(step["capability_id"])
            risk = self.risk_engine.assess(
                capability, step.get("arguments", {}),
                context=self.registry.risk_context(capability.id, step.get("arguments", {})),
            )
            step["risk"] = risk.level
            step["risk_reasons"] = list(risk.reasons)
            self.journal.append("goal.step.started", goal_id=goal.goal_id, mission_id=goal.mission_id,
                                capability_id=capability.id, status="running")
            if risk.requires_confirmation and request is None:
                confirmation = self.confirmations.create(capability.id, step["arguments"],
                    conversation_id=goal.conversation_id, generation_id=goal.goal_id,
                    messages=[], model_turns=goal.model_calls, action_count=goal.current_step)
                self._pending[confirmation.token] = confirmation
                durable = self.durable_confirmations.create(
                    goal_id=goal.goal_id, step_id=step["step_id"], capability_id=capability.id,
                    arguments=step["arguments"], ttl_seconds=max(0.0, confirmation.expires_at - monotonic()),
                )
                self._pending_durable[confirmation.token] = durable.confirmation_request_id
                step["status"] = "awaiting_confirmation"
                step["confirmation_token"] = confirmation.token
                step["confirmation"] = confirmation.public()
                checkpoint = dict(goal.checkpoint)
                checkpoint["outstanding_confirmation"] = {
                    "confirmation_request_id": durable.confirmation_request_id,
                    "step_id": step["step_id"],
                    "capability_id": capability.id,
                    "effect_fingerprint": durable.effect_fingerprint,
                }
                goal = self._save(goal, status="awaiting_confirmation", phase="execute", checkpoint=checkpoint)
                self.journal.append("goal.awaiting_confirmation", goal_id=goal.goal_id, mission_id=goal.mission_id,
                                    capability_id=capability.id, status="awaiting_confirmation")
                return goal
            if capability.id == "filesystem.write":
                goal = self._mark_mutation_state(goal, step, "STARTED_UNCERTAIN")
            is_discovery = not risk.requires_confirmation and risk.level == "low"
            try:
                result = self.registry.execute(capability.id, step["arguments"], confirmed=request is not None)
            except ConfirmationRequired:
                raise GoalStateError("confirmation_required") from None
            request = None
            if cancelled.is_set():
                # Cancellation may arrive while a blocking tool is in flight.
                # Do not pretend the action never happened.  Roll back a known
                # reversible transaction if possible; otherwise surface the
                # uncertain external effect for review.
                rollback_attempted, rollback_error = self._rollback_if_safer(capability.reversible, result.result)
                if rollback_error is not None:
                    return self._terminal(goal, "blocked", "cancel_rollback_failed")
                if result.status == "success" and not rollback_attempted and not is_discovery:
                    return self._terminal(goal, "blocked", "cancelled_after_action_requires_review")
                return self._terminal(goal, "cancelled", "cancel_requested")
            if capability.id == "filesystem.write" and result.status == "success":
                goal = self._mark_mutation_state(
                    goal, step, "COMPLETED", action_id=result.action_id,
                    transaction_id=(result.result or {}).get("transaction_id"),
                )
            goal = self._save(goal, discovery_actions=goal.discovery_actions + int(is_discovery),
                              mutating_actions=goal.mutating_actions + int(not is_discovery), phase="verify")
            verified = result.status == "success" and result.verified and self._matches(result.result, step["verification"])
            evidence = self._evidence(goal, step, result, verified)
            if verified:
                if capability.id == "filesystem.write":
                    goal = self._mark_mutation_state(
                        goal, step, "VERIFIED", action_id=result.action_id,
                        transaction_id=(result.result or {}).get("transaction_id"),
                    )
                step["status"] = "completed"
                step.pop("confirmation_token", None)
                step.pop("confirmation", None)
                checkpoint = dict(goal.checkpoint)
                checkpoint["completed_step_ids"] = [*checkpoint.get("completed_step_ids", []), step["step_id"]]
                checkpoint.update({"plan_version": goal.plan_version, "last_evidence_id": evidence["evidence_id"],
                                   "next_phase": "execute", "outstanding_confirmation": None})
                goal = self._save(goal, current_step=goal.current_step + 1,
                                  evidence=[*goal.evidence, evidence], checkpoint=checkpoint, phase="decide")
                self.journal.append("goal.step.completed", goal_id=goal.goal_id, mission_id=goal.mission_id, action_id=result.action_id,
                                    transaction_id=(result.result or {}).get("transaction_id"),
                                    capability_id=capability.id, status="completed")
                self._record_app_interaction(step, success=True)
                continue
            step["status"] = "failed"
            goal = self._save(goal, failed_steps=goal.failed_steps + 1,
                              evidence=[*goal.evidence, evidence], phase="decide")
            failure_category = result.error_category or "VERIFICATION_FAILED"
            metrics = dict(goal.metrics)
            strategy_failures = dict(metrics.get("strategy_failures") or {})
            strategy_key = action_fingerprint(capability.id, step.get("arguments", {}))
            same_strategy_failures = max(0, int(strategy_failures.get(strategy_key, 0) or 0)) + 1
            strategy_failures[strategy_key] = same_strategy_failures
            if len(strategy_failures) > 32:
                strategy_failures = dict(list(strategy_failures.items())[-32:])
            metrics["strategy_failures"] = strategy_failures
            uncertainty = self.uncertainty.assess({
                "verification_failed": not verified, "replans": goal.replans,
                "risk_level": risk.level, "irreversible": not capability.reversible,
                "same_strategy_failures": same_strategy_failures,
            })
            self.journal.append("goal.step.failed", goal_id=goal.goal_id, mission_id=goal.mission_id, action_id=result.action_id,
                                capability_id=capability.id, status="failed",
                                error_category=failure_category)
            self._record_app_interaction(step, success=False)
            recovery = self.recovery_policy.decide(
                failure_category=failure_category, same_strategy_failures=same_strategy_failures,
                reversible=capability.reversible, uncertainty_level=uncertainty.level)
            self.outcomes.record(ExecutionOutcome(
                objective=goal.objective, capability_id=capability.id, strategy=step.get("objective") or capability.id,
                success=False, verification="failed", failure_category=failure_category,
                recovery=recovery.action, attempts=same_strategy_failures,
                lesson=f"{failure_category}: prefer {recovery.action}; uncertainty={uncertainty.level}",
            ), reference=goal.goal_id)
            metrics["last_uncertainty"] = uncertainty.public()
            metrics["last_recovery"] = {"action": recovery.action, "reason": recovery.reason}
            deliberation_calls = 0
            if self.deliberation is not None:
                trigger, trigger_reasons = self.deliberation.should_trigger(
                    uncertainty_level=uncertainty.level, risk_level=risk.level, replans=goal.replans,
                    verification_failed=not verified)
                remaining_model_calls = max(0, self.budgets.max_model_calls - goal.model_calls)
                if trigger and remaining_model_calls > 0:
                    decision = self.deliberation.deliberate(
                        objective=goal.objective, evidence=evidence.get("summary", ""),
                        candidate_strategy=recovery.action, risk_level=risk.level,
                        uncertainty_reasons=trigger_reasons)
                    deliberation_calls = min(decision.calls, remaining_model_calls)
                    metrics["last_deliberation"] = {
                        "trigger_reasons": list(trigger_reasons), "calls": deliberation_calls,
                        "failed_calls": decision.failed_calls, "elapsed_ms": decision.elapsed_ms,
                        "confidence_band": decision.confidence_band,
                        "require_human": decision.require_human,
                        "require_more_observation": decision.require_more_observation,
                    }
                    self.journal.append("goal.deliberated", goal_id=goal.goal_id, mission_id=goal.mission_id,
                                        capability_id=capability.id, status="completed",
                                        duration_ms=decision.elapsed_ms,
                                        error_category=("provider_failure" if decision.failed_calls else None))
                elif trigger:
                    metrics["last_deliberation"] = {
                        "trigger_reasons": list(trigger_reasons), "calls": 0,
                        "skipped": "model_call_budget_exhausted",
                    }
            goal = self._save(goal, metrics=metrics, model_calls=goal.model_calls + deliberation_calls)
            rollback_attempted, rollback_error = self._rollback_if_safer(capability.reversible, result.result)
            if capability.id == "filesystem.write" and rollback_attempted and rollback_error is None:
                goal = self._mark_mutation_state(
                    goal, step, "ROLLED_BACK", action_id=result.action_id,
                    transaction_id=(result.result or {}).get("transaction_id"),
                )
            if rollback_error is not None:
                return self._terminal(goal, "blocked", "rollback_verification_failed")
            goal = self._replan_or_block(goal, result.error_category or "goal_verification_failed")
            if goal.status != "running":
                return goal
        # Failed evidence remains part of the audit trail after a successful
        # alternative path; it must not downgrade the newly verified result.
        current_ids = {step["step_id"] for step in goal.plan}
        verified_ids = {item["step_id"] for item in goal.evidence
                        if item["verification_state"] == "VERIFIED"}
        steps_verified = bool(current_ids) and current_ids <= verified_ids
        criterion_verified = self._success_criterion_verified(goal, verified_ids)
        status = "completed_verified" if steps_verified and criterion_verified else "completed_unverified"
        goal = self._terminal(goal, status, None)
        if status == "completed_verified":
            self._write_outcome_memory(goal)
        return goal

    def _record_app_interaction(self, step: dict[str, Any], *, success: bool) -> None:
        if self.application_memory is None or not str(step.get("capability_id") or "").startswith("computer.ui."):
            return
        try:
            context = self.registry.risk_context(step["capability_id"], step.get("arguments", {}))
            application = str(context.get("application") or "")
            structural = str(context.get("structural_fingerprint") or "")
            if not application or not structural:
                return
            app_id = ApplicationMemory.app_identity(
                executable=application, app_name=application,
                title_family=str(context.get("window_title") or "")[:120],
            )
            self.application_memory.record(
                app_identity=app_id,
                intent=str(step.get("objective") or step.get("expected_evidence") or step["capability_id"])[:500],
                target_label=str(context.get("target_name") or "")[:160],
                control_type=str(context.get("target_role") or "")[:80],
                structural_fingerprint=structural,
                action_type=step["capability_id"].rsplit(".", 1)[-1],
                success=success,
            )
        except Exception:
            # Interaction memory is advisory. It can never make execution fail.
            return

    @staticmethod
    def _success_criterion_verified(goal: GoalExecution, verified_ids: set[str]) -> bool:
        """Require exact readback evidence for a backend-derived file criterion."""
        required = goal.checkpoint.get("required_readback")
        if isinstance(required, dict):
            return any(
                step["capability_id"] == "filesystem.read"
                and step["step_id"] in verified_ids
                and step["arguments"].get("path") == required.get("path")
                and step["verification"].get("content") == required.get("content")
                for step in goal.plan
            )
        derived = InitialGoalPlanner.derive_success_criterion(goal.plan)
        if derived != goal.success_criteria:
            return True
        if derived == "All planned capability steps pass authoritative backend verification.":
            return True
        for step in goal.plan:
            if (step["capability_id"] == "filesystem.read"
                    and step["step_id"] in verified_ids
                    and step["verification"].get("content") is not None):
                path = step["arguments"].get("path")
                content = step["verification"].get("content")
                if derived == (f"{path} exists in the allowed workspace and its content equals "
                               f"{content}"):
                    return True
        return False

    @staticmethod
    def _matches(result: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
        if not expected:
            return True
        value: Any = result or {}
        for dotted, wanted in expected.items():
            value = result or {}
            for part in dotted.split("."):
                if not isinstance(value, dict) or part not in value:
                    return False
                value = value[part]
            if value != wanted:
                return False
        return True

    def _evidence(self, goal: GoalExecution, step: dict[str, Any], result: Any, verified: bool) -> dict[str, Any]:
        observation = compact_observation(result.capability_id, result)
        observation.pop("content", None); observation.pop("diff_summary", None)
        source = "SEMANTIC_UIA" if result.capability_id.startswith("computer.ui.") else (
            "VISUAL_MODEL" if result.capability_id == "computer.visual.analyze" else "DETERMINISTIC")
        return {"evidence_id": uuid4().hex, "source": source, "provenance": source,
            "step_id": step["step_id"], "summary": _compact(observation),
            "verification_state": "VERIFIED" if verified else "FAILED", "timestamp": _now(),
            "capability_id": result.capability_id, "action_id": result.action_id}

    def _replan_or_block(self, goal: GoalExecution, reason: str) -> GoalExecution:
        if goal.failed_steps >= self.budgets.max_failed_steps or goal.replans >= self.budgets.max_replans:
            return self._terminal(goal, "blocked", reason)
        if self.planner is None and self.initial_planner is None:
            return self._terminal(goal, "blocked", reason)
        remaining_calls = self.budgets.max_model_calls - goal.model_calls
        if remaining_calls < 1:
            return self._terminal(goal, "blocked", "model_call_budget_exceeded")
        package = self.context.build(goal.objective, conversation_id=goal.conversation_id)
        failed = goal.plan[goal.current_step]
        failure_context = (
            f"Previous action failed: {failed['capability_id']} "
            f"target={str(failed.get('arguments', {}).get('path', ''))[:160]}; "
            f"reason={reason}; observation={goal.evidence[-1]['summary'][:500] if goal.evidence else 'none'}. "
            "Choose a different action or obtain new evidence before retrying. "
            "The original objective and success condition remain in force."
        )
        try:
            if self.planner is not None:
                raw_plan = self.planner.replan(goal.objective, failure_context + "\n" + package.content, goal)
                used_calls = 1
            else:
                from .context_builder import ContextPackage
                replanned = self.initial_planner.plan(
                    goal.objective,
                    ContextPackage(failure_context + "\n" + package.content, 0, (), ()),
                    max_model_calls=remaining_calls, conversation_id=goal.conversation_id,
                    mission_id=goal.mission_id,
                )
                raw_plan = replanned.semantic_steps()
                used_calls = replanned.model_calls
            plan = self._validate_plan(raw_plan)
        except (InitialPlanInvalid, ValueError, WorkspacePathError):
            return self._terminal(goal, "blocked", "replan_invalid")
        if not plan:
            return self._terminal(goal, "blocked", "replan_empty")
        if (action_fingerprint(plan[0]["capability_id"], plan[0]["arguments"])
                == action_fingerprint(failed["capability_id"], failed["arguments"])):
            return self._terminal(goal, "blocked", "repeated_failed_action")
        checkpoint = dict(goal.checkpoint)
        checkpoint.update({"plan_version": goal.plan_version + 1, "reason_for_replan": reason,
                           "supersedes": goal.plan_version, "next_phase": "execute"})
        goal = self._save(goal, status="running", phase="execute", plan=plan, current_step=0,
                          plan_version=goal.plan_version + 1, replans=goal.replans + 1,
                          model_calls=goal.model_calls + used_calls, checkpoint=checkpoint)
        self.journal.append("goal.replanned", goal_id=goal.goal_id, mission_id=goal.mission_id, status="running", error_category=reason)
        return goal

    def _refresh_after_restart(self, goal: GoalExecution) -> GoalExecution:
        if goal.current_step >= len(goal.plan):
            return self._save(goal, blockers=[])
        step = goal.plan[goal.current_step]
        stale = TRANSIENT_ARGUMENTS.intersection(step.get("arguments", {}))
        if not stale:
            return self._save(goal, blockers=[])
        refresh_capability = "computer.windows" if "window_ref" in stale else (
            "computer.visual.displays" if "image_ref" in stale else "computer.observe")
        result = self.registry.execute(refresh_capability, {})
        evidence = self._evidence(goal, {"step_id": "resume-refresh"}, result, result.status == "success" and result.verified)
        goal = self._save(goal, evidence=[*goal.evidence, evidence], blockers=[])
        return self._replan_or_block(goal, "stale_transient_reference")

    def _rollback_if_safer(self, reversible: bool, result: dict[str, Any] | None) -> tuple[bool, str | None]:
        transaction_id = (result or {}).get("transaction_id")
        if not reversible or not transaction_id or self.registry.transactions is None:
            return False, None
        try:
            rolled_back = self.registry.transactions.rollback(transaction_id)
        except TransactionError as error:
            self.journal.append("goal.rollback.failed", transaction_id=str(transaction_id),
                                status="failed", error_category=str(error))
            return True, str(error)
        self.journal.append("goal.rollback.verified", transaction_id=str(transaction_id),
                            status="rolled_back")
        return True, None

    def _budget_exhausted(self, goal: GoalExecution) -> bool:
        elapsed = monotonic() - self._started.get(goal.goal_id, monotonic())
        return (goal.failed_steps >= self.budgets.max_failed_steps or goal.replans > self.budgets.max_replans
                or goal.mutating_actions >= self.budgets.max_mutating_actions
                or goal.discovery_actions >= self.budgets.max_discovery_actions
                or goal.model_calls > self.budgets.max_model_calls or elapsed > self.budgets.max_duration_seconds)

    def pause(self, goal_id: str) -> GoalExecution:
        goal = self.store.get(goal_id)
        if goal.status in {"completed_verified", "completed_unverified", "failed", "blocked", "cancelled"}:
            return goal
        goal = self._save(goal, status="paused", phase="decide")
        self.journal.append("goal.paused", goal_id=goal.goal_id, mission_id=goal.mission_id, status="paused")
        return goal

    def cancel(self, goal_id: str) -> GoalExecution:
        goal = self.store.get(goal_id)
        if goal.status in {"completed_verified", "completed_unverified", "failed", "blocked", "cancelled"}:
            return goal
        self._cancelled.setdefault(goal_id, Event()).set()
        if goal.status == "running":
            self.journal.append("goal.cancel_requested", goal_id=goal.goal_id, mission_id=goal.mission_id,
                                status="running")
            return goal
        goal = self._save(goal, status="cancelled", phase="terminal")
        self.journal.append("goal.cancelled", goal_id=goal.goal_id, mission_id=goal.mission_id, status="cancelled")
        return goal

    def _terminal(self, goal: GoalExecution, status: GoalStatus, reason: str | None) -> GoalExecution:
        blockers = [*goal.blockers, reason] if reason and reason not in goal.blockers else goal.blockers
        elapsed_ms = int((monotonic() - self._started.get(goal.goal_id, monotonic())) * 1000)
        metrics = {**goal.metrics, "elapsed_ms": elapsed_ms, "replans": goal.replans,
                   "discovery_actions": goal.discovery_actions, "mutating_actions": goal.mutating_actions,
                   "model_calls": goal.model_calls}
        goal = self._save(goal, status=status, phase="terminal", blockers=blockers, metrics=metrics)
        event = "goal.completed" if status.startswith("completed") else "goal.blocked" if status == "blocked" else f"goal.{status}"
        self.journal.append(event, goal_id=goal.goal_id, mission_id=goal.mission_id, conversation_id=goal.conversation_id,
                            status=status, error_category=reason)
        return goal

    def _write_outcome_memory(self, goal: GoalExecution) -> None:
        summaries = [e["summary"] for e in goal.evidence if e["verification_state"] == "VERIFIED"][-3:]
        if not summaries:
            return
        capability_id = goal.plan[-1]["capability_id"] if goal.plan else "goal"
        strategy = " -> ".join(step.get("capability_id", "") for step in goal.plan)[:500]
        self.outcomes.record(ExecutionOutcome(
            objective=goal.objective, capability_id=capability_id, strategy=strategy, success=True,
            verification="; ".join(summaries)[:700], recovery=None, attempts=max(1, goal.failed_steps + 1),
            lesson="Verified strategy completed successfully.",
        ), reference=goal.goal_id)

    def _save(self, goal: GoalExecution, **changes: Any) -> GoalExecution:
        return self.store.save(replace(goal, updated_at=_now(), **changes))
