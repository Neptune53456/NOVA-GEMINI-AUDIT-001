"""Politique de chemins commune aux agents d'ingénierie.

Les agents peuvent travailler sur le code public du repository, mais les surfaces
qui contiennent évaluations, historiques, backups ou artefacts temporaires restent
hors de leur contexte LLM et hors de leur périmètre d'écriture. Cette politique est
centralisée pour éviter qu'un Planner autorise un chemin qu'un Developer refuse (ou
inversement).
"""

from __future__ import annotations

from pathlib import Path
import re


HOLDOUT_NAME = ".self_improvement_holdout"

MODEL_PRIVATE_DIR_NAMES = frozenset({
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".hypothesis",
    ".temp_tests",
    ".self_improvement_worktrees",
    ".self_improvement_discoveries",
    ".self_improvement_recovery",
    ".self_improvement_supervisor_recovery",
    ".runtime",
    ".continue",
    ".vscode",
    ".idea",
    ".cursor",
    ".cline",
    ".roo",
    "benchmark_results",
    "backups",
    HOLDOUT_NAME,
})

# Préfixes spécifiques au projet qui ne doivent pas être lus/édités par le LLM.
MODEL_PRIVATE_FILE_NAMES = frozenset({
    ".env",
    "credentials.json",
    "credentials.toml",
    "secrets.json",
    "secrets.toml",
    "secrets.yaml",
    "secrets.yml",
})

MODEL_PRIVATE_PREFIXES = (
    "self_improvement/reports/",
    # Jeux d'évaluation : le modèle reçoit uniquement les cas TRAIN sélectionnés
    # par AutonomousSelfImprovementAgent, jamais le corpus brut qui contient le
    # garde de généralisation.
    "self_improvement/scenarios/",
    "self_improvement/bugs/",
    "self_improvement/red_team_corpus/",
)

# Trusted Computing Base (TCB) : lisible pour comprendre l'architecture, mais
# jamais modifiable par l'agent lui-même. Sinon une auto-amélioration pourrait
# gagner en abaissant le juge, le benchmark, les budgets ou le rollback.
AGENT_WRITE_PROTECTED_PATHS = frozenset({
    # Configuration / bootstrap racine.
    "pyproject.toml",
    "AGENTS.md",
    "conftest.py",
    # Trusted Control Plane (TCB). Ces modules imposent les frontières de
    # sécurité, budgets, transactions, rollback et mesure. Ils restent
    # volontairement hors auto-modification afin que l'agent ne puisse pas
    # s'accorder lui-même de nouveaux privilèges ni changer son examen.
    "file_editor.py",
    "self_improvement/agent_doctor.py",
    "self_improvement/agent_path_policy.py",
    "self_improvement/objective_preflight.py",
    "self_improvement/symbol_contract.py",
    "self_improvement/public_workspace_resources.py",
    "self_improvement/agent_code_safety.py",
    "self_improvement/agent_runtime.py",
    "self_improvement/trusted_supervisor.py",
    "self_improvement/execution_limits.py",
    "self_improvement/benchmark_runner.py",
    "self_improvement/campaign_manager.py",
    "self_improvement/engineering_orchestrator.py",
    "self_improvement/evaluator.py",
    "self_improvement/improvement_orchestrator.py",
    "self_improvement/judge_engine.py",
    "self_improvement/models.py",
    "self_improvement/process_safety.py",
    "self_improvement/scenario_loader.py",
    "self_improvement/adaptive_budget.py",
    "self_improvement/engineering_memory.py",
    "self_improvement/learning_curriculum.py",
    "self_improvement/dependency_guard.py",
    "self_improvement/regression_test_guard.py",
    "self_improvement/developer_docs.py",
    "self_improvement/trusted_git_checkpoint.py",
    "self_improvement/sandbox_executor.py",
    "self_improvement/goal_orchestrator.py",
})


AGENT_RESERVED_BASENAMES = frozenset({
    "conftest.py",
    "pytest.py",
    "coverage.py",
    "sitecustomize.py",
    "usercustomize.py",
    "pytest.ini",
    "tox.ini",
})

AGENT_EDITABLE_SUFFIXES = frozenset({
    ".py", ".txt", ".json", ".md", ".ini", ".yaml", ".yml", ".toml",
})


def normalize_relative_path(relative: str | Path) -> str:
    text = Path(relative).as_posix()
    while text.startswith("./"):
        text = text[2:]
    return text


def normalize_repo_path(relative: str | Path) -> str:
    """Normalise les separateurs sans supprimer un prefixe de package."""
    raw = str(relative or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("repo_path_invalid")
    while raw.startswith("./"):
        raw = raw[2:]
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("repo_path_traversal")
    return "/".join(parts)


def resolve_repo_path(repo_root: str | Path, relative: str | Path, *, must_exist: bool = False) -> Path:
    root = Path(repo_root).resolve()
    normalized = normalize_repo_path(relative)
    resolved = (root / Path(*normalized.split("/"))).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("repo_path_outside_workspace") from exc
    if must_exist and not resolved.is_file():
        raise FileNotFoundError(normalized)
    return resolved


def validate_target_files(
    repo_root: str | Path,
    paths: list[str],
    *,
    allow_missing: bool = True,
) -> list[str]:
    """Valide et deduplique des cibles en preservant leurs chemins relatifs."""
    normalized: list[str] = []
    for raw in paths:
        rel = normalize_repo_path(raw)
        resolve_repo_path(repo_root, rel, must_exist=not allow_missing)
        if is_model_private_path(rel) or not is_agent_editable_path(rel):
            raise ValueError(f"repo_path_forbidden:{rel}")
        if rel not in normalized:
            normalized.append(rel)
    return normalized


def is_model_private_path(relative: str | Path) -> bool:
    """Vrai si ce chemin ne doit jamais entrer dans le contexte ou les écritures LLM."""
    rel = normalize_relative_path(relative)
    lowered = rel.casefold()
    parts = [part.casefold() for part in Path(rel).parts]
    if any(part in MODEL_PRIVATE_DIR_NAMES for part in parts):
        return True
    if any("holdout" in part for part in parts):
        return True
    basename = Path(rel).name.casefold()
    if basename in MODEL_PRIVATE_FILE_NAMES or basename.startswith(".env"):
        return True
    return any(lowered.startswith(prefix.casefold()) for prefix in MODEL_PRIVATE_PREFIXES)


def is_agent_write_protected_path(relative: str | Path) -> bool:
    rel = normalize_relative_path(relative).casefold()
    if Path(rel).name.casefold() in AGENT_RESERVED_BASENAMES:
        return True
    return rel in {item.casefold() for item in AGENT_WRITE_PROTECTED_PATHS}


def is_agent_readable_path(relative: str | Path) -> bool:
    """Vrai pour les fichiers texte que le modèle peut inspecter en lecture seule."""
    rel = normalize_relative_path(relative)
    if is_model_private_path(rel):
        return False
    return Path(rel).suffix.casefold() in AGENT_EDITABLE_SUFFIXES


def is_agent_editable_path(relative: str | Path) -> bool:
    """Vrai uniquement si le modèle peut aussi proposer une écriture sur ce chemin."""
    rel = normalize_relative_path(relative)
    if not is_agent_readable_path(rel) or is_agent_write_protected_path(rel):
        return False
    return True



# Modules cognitifs explicitement autorisés à évoluer lors d'une auto-amélioration.
# Cette liste n'est pas une permission d'écriture à elle seule : la politique
# générale, le Planner et le superviseur de confiance continuent de valider les
# chemins. Elle documente la frontière entre « cerveau modifiable » et TCB.
SELF_MODIFIABLE_AGENT_PATHS = frozenset({
    "self_improvement/developer_agent.py",
    "self_improvement/developer_tools.py",
    "self_improvement/engineering_planner.py",
    "self_improvement/repo_intelligence.py",
    "self_improvement/developer_strategy.py",
    "self_improvement/code_localization.py",
    "self_improvement/planning_strategy.py",
    "self_improvement/autonomous_engineer.py",
    "self_improvement/engineering_reviewer.py",
    "self_improvement/confidence_calibration.py",
    "self_improvement/mission_manager.py",
    "self_improvement/meta_learning.py",
    "self_improvement/semantic_memory.py",
    "self_improvement/consensus_engine.py",
    "self_improvement/dynamic_red_team.py",
    "model_router.py",
    "conversation_manager.py",
    "request_interpreter.py",
    "action_planner.py",
})


def is_trusted_control_path(relative: str | Path) -> bool:
    """Alias explicite utilisé par le superviseur d'auto-amélioration."""
    return is_agent_write_protected_path(relative)

def is_non_executable_audit_artifact(relative: str | Path) -> bool:
    """Artefacts de rapport pouvant survivre au rollback sans créer de code exécutable."""
    rel = normalize_relative_path(relative)
    lowered = rel.casefold()
    if not lowered.startswith("self_improvement/reports/"):
        return False
    return Path(rel).suffix.casefold() in {".json", ".jsonl", ".md", ".txt"}
