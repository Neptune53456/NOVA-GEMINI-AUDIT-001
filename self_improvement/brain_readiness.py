"""Readiness V7 du chemin cognitif, avec degradation explicite."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from file_editor import _compact_edit_context
from model_router import ollama_status, omniroute_status, provider_health_snapshot, routing_diagnostic
from self_improvement.context_budget import estimate_text_tokens
from self_improvement.engineering_planner import EngineeringObjective, EngineeringPlanner


@dataclass(frozen=True)
class BrainReadinessReport:
    structural_ready: bool
    goal: str
    planner_prompt_tokens_estimate: int
    planner_budget_tokens: int
    planner_context_chars: int
    relevant_router_visible: bool
    related_tests_visible: bool
    edit_context_tokens_estimate: int | None
    configured_remote_providers: list[str]
    notes: list[str]
    status: str = "NOT_READY"
    checks: dict[str, object] | None = None

    def to_dict(self):
        return asdict(self)


def run_brain_readiness(repo_root: str | Path, goal: str = "Ajoute ExampleProvider V1") -> BrainReadinessReport:
    root = Path(repo_root).resolve()
    planner = EngineeringPlanner(root)
    objective = EngineeringObjective(goal=goal)
    context = planner._get_repo_context(objective)
    prompt = planner._build_prompt(objective, context)
    fitted, _ = planner._fit_prompt_budget(objective, prompt, context)
    prompt_tokens = estimate_text_tokens(fitted)

    router = root / "model_router.py"
    edit_tokens = None
    if router.is_file():
        compact = _compact_edit_context(router.read_text(encoding="utf-8", errors="replace"), goal)
        edit_tokens = estimate_text_tokens(compact)

    snapshot = provider_health_snapshot()
    configured = [name for name, info in snapshot.items() if info.get("configured") and name != "local"]
    router_visible = "model_router.py" in fitted
    tests_visible = "test_model_router_responses.py" in fitted or "test_v62_model_infrastructure.py" in fitted
    structural = bool(
        prompt_tokens <= planner.planner_budget.max_input_tokens
        and router_visible and tests_visible and edit_tokens is not None and edit_tokens <= 4700
    )

    omni = omniroute_status(force=True)
    ollama = ollama_status()
    routing = None
    if omni.get("models_count", 0):
        try:
            routing = routing_diagnostic("planning")
        except Exception as exc:
            routing = {"selected": None, "error_kind": type(exc).__name__}
    direct_ready = bool(configured)
    cognitive_ready = bool(omni.get("healthy") or direct_ready or ollama.get("healthy"))
    status = "READY" if structural and omni.get("healthy") else ("DEGRADED" if structural and cognitive_ready else "NOT_READY")
    notes = [
        "structural_ready valide le chemin critique local; les services tiers restent externes.",
        "Un provider secondaire indisponible ne bloque pas les routes alternatives.",
    ]
    checks = {
        "omniroute": omni,
        "model_discovery": {"ok": bool(omni.get("models_count")), "count": omni.get("models_count", 0)},
        "model_catalog": {"ok": bool(omni.get("models_count"))},
        "routing": {"ok": bool(routing and routing.get("selected")), "diagnostic": routing},
        "health_manager": {"ok": True},
        "fallback": {"ok": cognitive_ready},
        "token_budgeting": {"ok": prompt_tokens <= planner.planner_budget.max_input_tokens},
        "direct_providers": {"ok": direct_ready, "configured": configured},
        "ollama": ollama,
    }
    return BrainReadinessReport(
        structural_ready=structural, goal=goal,
        planner_prompt_tokens_estimate=prompt_tokens,
        planner_budget_tokens=planner.planner_budget.max_input_tokens,
        planner_context_chars=len(context), relevant_router_visible=router_visible,
        related_tests_visible=tests_visible, edit_context_tokens_estimate=edit_tokens,
        configured_remote_providers=configured, notes=notes, status=status, checks=checks,
    )
