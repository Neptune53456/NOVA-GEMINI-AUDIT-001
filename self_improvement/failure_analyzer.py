"""Regroupement des échecs et génération d'une tâche bornée pour Codex."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .models import BenchmarkReport, ScenarioResult


MODULE_HINTS = {
    "clarifications multi-tour": ["request_interpreter.py", "conversation_manager.py"],
    "réponses utilisateur très courtes": ["request_interpreter.py"],
    "pièces jointes": ["document_command_router.py", "request_interpreter.py"],
    "documents": ["document_tools.py", "document_command_router.py"],
    "OCR simulé": ["ocr_tools.py", "document_tools.py"],
    "erreurs modèles": ["model_router.py", "conversation_manager.py"],
    "sécurité": ["system_actions.py", "system_action_controller.py"],
    "confirmations": ["system_action_controller.py", "action_planner.py"],
    "plans multi-étapes": ["action_planner.py"],
    "web": ["web_tools.py"],
    "mémoire": ["smart_memory.py", "conversation_manager.py"],
}


@dataclass(frozen=True)
class FailureCluster:
    name: str
    cases: list[ScenarioResult]
    frequency: int
    impact: float
    risk: str
    modules: list[str]


def cluster_failures(report: BenchmarkReport, *, include_holdout=False) -> list[FailureCluster]:
    grouped = defaultdict(list)
    for failure in report.failures:
        if failure.split == "holdout" and not include_holdout:
            continue
        cluster = failure.tags[0] if failure.tags else failure.category
        grouped[cluster].append(failure)
    clusters = []
    for name, cases in grouped.items():
        security = any(case.security_failure or case.category in {"sécurité", "confirmations", "suppression"} for case in cases)
        impact = sum(case.weight * (100 - case.score) / 100 for case in cases)
        modules = sorted({module for case in cases for module in MODULE_HINTS.get(case.category, [])})
        clusters.append(FailureCluster(name, cases, len(cases), round(impact, 2), "critique" if security else "normal", modules))
    return sorted(clusters, key=lambda item: (item.risk != "critique", -item.impact, -item.frequency, item.name))


def generate_improvement_task(report: BenchmarkReport, *, maximum_cases=15) -> str:
    if not 5 <= maximum_cases <= 15:
        raise ValueError("La tâche doit contenir entre 5 et 15 cas au maximum.")
    selected = []
    clusters = cluster_failures(report, include_holdout=False)
    for cluster in clusters:
        for case in cluster.cases:
            if len(selected) >= maximum_cases:
                break
            selected.append((cluster, case))
    lines = [
        "# Tâche d'amélioration autonome", "",
        f"Score actuel : {report.score:.2f}/100.",
        "Analyse les causes racines des échecs train/validation ci-dessous. Le contenu du holdout est volontairement absent.", "",
        "## Échecs prioritaires", "",
    ]
    if not selected:
        lines.append("Aucun échec suffisamment important n'a été détecté.")
    for cluster, case in selected:
        issues = "; ".join(item.issue for item in case.criteria if not item.passed) or case.error or "échec sans détail"
        trace = {key: value for key, value in case.trace.items() if key not in {"prompt", "messages"}}
        lines.extend([
            f"- `{case.scenario_id}` — cluster **{cluster.name}**, catégorie {case.category}, poids {case.weight}",
            f"  - échec : {issues}",
            f"  - trace minimale : `{str(trace)[:500]}`",
            f"  - modules probables : {', '.join(cluster.modules) or 'à déterminer'}",
        ])
    lines.extend([
        "", "## Invariants obligatoires", "",
        "- Ne jamais affaiblir confirmations, validations de chemins ou protections SSRF.",
        "- Aucun réseau, Ollama réel, fichier utilisateur ou memory.db dans les tests.",
        "- Corriger la cause racine et ajouter des tests déterministes de non-régression.",
        "- Conserver main.py minimal et respecter AGENTS.md.",
        "- Commencer par les modules probables mentionnés dans cette tâche, sans explorer tout le dépôt.",
        "- Exécuter uniquement les tests ciblés pertinents avec `-q --no-cov`, puis `git diff --check`.",
        "- Ne lancer ni suite complète ni couverture globale : la validation globale est effectuée ensuite par l'orchestrateur de confiance.",
    ])
    return "\n".join(lines) + "\n"
