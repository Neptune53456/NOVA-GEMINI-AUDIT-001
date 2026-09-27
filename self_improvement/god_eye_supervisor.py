"""God Eyes campaign adapter over the existing trusted supervisor transaction."""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable

from self_improvement.engineering_planner import EngineeringObjective
from self_improvement.god_eye_benchmark import (GodEyeImprovementBudget, judge_god_eye_candidate,
                                                 validate_change_scope)
from self_improvement.trusted_supervisor import TrustedRepositorySnapshot, TrustedSelfImprovementSupervisor

Stage = Callable[[], dict[str, Any]]

class GodEyeSupervisorWorkflow:
    """Runs one resumable challenger; all acceptance signals are structured evidence."""
    def __init__(self, supervisor: TrustedSelfImprovementSupervisor, store) -> None:
        self.supervisor, self.store = supervisor, store

    def run(self, goal: str, *, planned_paths: list[str], tests: Stage, benchmark: Stage,
            validation: Stage, locked_holdout: Stage, incumbent: dict[str, Any],
            budget: GodEyeImprovementBudget | None = None) -> dict[str, Any]:
        campaign_id = sha256(goal.encode()).hexdigest()[:20]
        previous = next((v for v in self.store.governance("god_eye_campaign")
                         if v.get("campaign_id") == campaign_id), None)
        if previous and previous.get("status") in {"accepted", "rejected"}: return previous
        scope = validate_change_scope(planned_paths)
        if not scope["accepted"]: return self._blocked(campaign_id, goal, scope)
        cap = budget or GodEyeImprovementBudget()
        admitted = cap.check(paths=planned_paths)
        if not admitted["accepted"]: return self._blocked(campaign_id, goal, admitted)
        active = [v for v in self.store.governance("god_eye_campaign") if v.get("status") == "running"]
        if active and all(v.get("campaign_id") != campaign_id for v in active):
            return self._blocked(campaign_id, goal, {"reason": "another_challenger_is_active"})
        transaction = TrustedRepositorySnapshot(self.supervisor.repo_root)
        objective = EngineeringObjective(goal, constraints=["God Eyes paper-only", "Respect strict allowlist"],
            metadata={"god_eye": True, "campaign_id": campaign_id, "planned_paths": planned_paths})
        state = {"campaign_id": campaign_id, "goal": goal, "status": "running", "stage": "proposal",
                 "started_at": datetime.now(timezone.utc).isoformat(), "planned_paths": planned_paths}
        self._save(state)
        try:
            transaction.capture_repository(); self.supervisor.recovery.create(transaction, objective)
            state["stage"] = "isolated_implementation"; self._save(state)
            worker = self.supervisor.engineering_runner(objective)
            changed = transaction.changed_paths(); scope = validate_change_scope(changed)
            usage = worker.get("worker_usage", {}) if isinstance(worker, dict) else {}
            budget_result = cap.check(paths=changed, diff_lines=int(usage.get("diff_lines", 0)),
                model_calls=int(usage.get("model_calls_total", 0)), tokens=int(usage.get("tokens", 0)))
            if not scope["accepted"] or not budget_result["accepted"]:
                raise ValueError("forbidden_or_over_budget_change")
            evidence: dict[str, Any] = {}
            for name, action in (("targeted_tests", tests), ("benchmark", benchmark),
                                 ("validation", validation), ("locked_holdout", locked_holdout)):
                state["stage"] = name; self._save(state); evidence[name] = action()
            candidate = {**evidence.get("benchmark", {}), **evidence.get("validation", {}),
                         "tests_passed": bool(evidence["targeted_tests"].get("passed")),
                         "validation_passed": bool(evidence["validation"].get("passed")),
                         "holdout_passed": bool(evidence["locked_holdout"].get("passed"))}
            decision = judge_god_eye_candidate(candidate, incumbent)
            state.update(stage="judge", judge=decision, evidence=evidence, changed_paths=changed)
            if not decision["accepted"]:
                self.supervisor._rollback(transaction); state.update(status="rejected", rollback_performed=True)
            else:
                self.supervisor.recovery.clear(); state.update(status="accepted", rollback_performed=False)
                previous_champion=next((v for v in reversed(self.store.governance("trader_champion")) if v.get("status")=="active"),None)
                proposed=dict(evidence.get("benchmark",{}).get("trader_config",{}))
                allowed={"max_position_fraction","max_gross_exposure","cash_reserve_fraction","liquidity_fraction",
                         "warning_drawdown","defensive_drawdown","halt_drawdown","switch_threshold",
                         "action_cooldown_seconds","minimum_action_fraction"}
                config={k:v for k,v in proposed.items() if k in allowed}
                champion={"kind":"trader_champion","status":"active","version":campaign_id,
                    "previous_champion":previous_champion.get("version") if previous_champion else None,
                    "trader_config":config,"judge":decision,"metrics":candidate,"activated_at":datetime.now(timezone.utc).isoformat(),
                    "paper_only":True,"risk_engine_required":True,"raw_holdout_exposed":False}
                self.store.save_governance("trader_champion",campaign_id,"1",champion,champion["activated_at"])
                state["promotion"]={"previous_champion":champion["previous_champion"],"new_champion":campaign_id,
                    "activated_at":champion["activated_at"]}
            state["finished_at"] = datetime.now(timezone.utc).isoformat(); self._save(state); return state
        except Exception as error:
            try: self.supervisor._rollback(transaction)
            finally:
                state.update(status="rejected", stage="rollback", rollback_performed=True,
                             error=type(error).__name__, finished_at=datetime.now(timezone.utc).isoformat())
                self._save(state)
            return state

    def rollback_trader_champion(self,current_version:str,reason:str)->dict[str,Any]:
        champions=self.store.governance("trader_champion")
        current=next((v for v in champions if v.get("version")==current_version and v.get("status")=="active"),None)
        if not current or not current.get("previous_champion"): raise ValueError("rollback_target_unavailable")
        target=next((v for v in champions if v.get("version")==current["previous_champion"]),None)
        if target is None: raise ValueError("rollback_target_unavailable")
        now=datetime.now(timezone.utc).isoformat(); value={**target,"status":"active","rollback_from":current_version,
            "rollback_reason":reason,"activated_at":now,"paper_only":True}
        self.store.save_governance("trader_champion",str(target["version"]),f"rollback-{sha256(now.encode()).hexdigest()[:12]}",value,now)
        return value

    def _blocked(self, campaign_id: str, goal: str, details: dict[str, Any]) -> dict[str, Any]:
        value = {"campaign_id": campaign_id, "goal": goal, "status": "rejected", "stage": "policy",
                 "blocked_unsafe_modification": True, "details": details,
                 "finished_at": datetime.now(timezone.utc).isoformat()}
        self._save(value); return value

    def _save(self, value: dict[str, Any]) -> None:
        # Each stage is append-only; campaign snapshots never rewrite model history.
        self.store.save_governance("god_eye_campaign", value["campaign_id"], str(value["stage"]), value,
                                   datetime.now(timezone.utc).isoformat())
