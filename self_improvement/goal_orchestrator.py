"""Goal Orchestrator V1 : point de routage supérieur de l'agent général.

Il ne remplace pas les spécialistes. Il choisit le bon domaine puis délègue :
EngineeringOrchestrator pour le code, routeurs déterministes pour documents/système,
et conversation pour le reste. Les actions sensibles restent soumises aux garde-fous
des spécialistes.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import re
from typing import Any

@dataclass(frozen=True)
class GoalRoute:
    domain: str
    confidence: float
    reason: str
    requires_execution: bool = False
    def to_dict(self): return asdict(self)

class GoalOrchestrator:
    ENGINEERING = ("code", "python", "bug", "test", "refactor", "provider", "api", "repository", "repo", "module", "fonction", "classe")
    DOCUMENT = ("document", "pdf", "docx", "fichier texte", "résume", "resume")
    SYSTEM = ("cpu", "ram", "disque", "windows", "système", "systeme", "heure", "date", "pc")

    def classify(self, goal: str) -> GoalRoute:
        text = re.sub(r"\s+", " ", str(goal or "").casefold()).strip()
        if not text: raise ValueError("goal_empty")
        scores = {
            "engineering": sum(k in text for k in self.ENGINEERING),
            "document": sum(k in text for k in self.DOCUMENT),
            "system": sum(k in text for k in self.SYSTEM),
        }
        domain, score = max(scores.items(), key=lambda x: x[1])
        if score == 0:
            return GoalRoute("conversation", 0.55, "aucun spécialiste déterministe dominant", False)
        total = sum(scores.values()) or 1
        confidence = min(0.98, 0.62 + 0.12 * score + 0.08 * score / total)
        return GoalRoute(domain, round(confidence, 3), f"signaux {domain}: {score}", True)

    def run(self, goal: str, *, software_agent: Any = None) -> dict[str, Any]:
        route = self.classify(goal)
        if route.domain == "engineering":
            if software_agent is None: raise ValueError("software_agent_required")
            result = software_agent.run_objective(goal)
            return {"route": route.to_dict(), "delegated": True, "result": result}
        if route.domain in {"document", "system"}:
            from command_router import handle_direct_command
            result = handle_direct_command(goal)
            return {"route": route.to_dict(), "delegated": bool(result.get("handled")), "result": result}
        return {"route": route.to_dict(), "delegated": False, "result": None}
