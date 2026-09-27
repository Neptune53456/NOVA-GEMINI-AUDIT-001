"""Diagnostic local et non destructif du Software Agent.

Aucun appel réseau et aucune valeur de secret ne sont affichés. Le doctor vérifie
uniquement que l'environnement, le repository et les garde-fous nécessaires sont
présents avant un chantier autonome.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class DoctorReport:
    ready: bool
    checks: list[DoctorCheck] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    status: str = "NOT_READY"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": [item.to_dict() for item in self.checks],
            "details": self.details,
            "status": self.status,
        }


def run_doctor(repo_root: str | Path | None = None) -> DoctorReport:
    root = Path(repo_root or Path.cwd()).resolve()
    checks: list[DoctorCheck] = []

    version_ok = sys.version_info >= (3, 10)
    checks.append(DoctorCheck(
        "python",
        "PASS" if version_ok else "FAIL",
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    ))

    required_paths = [
        root / "model_router.py",
        root / "self_improvement" / "agent_runtime.py",
        root / "self_improvement" / "engineering_orchestrator.py",
        root / "self_improvement" / "trusted_supervisor.py",
        root / "self_improvement" / "developer_agent.py",
        root / "requirements.txt",
    ]
    missing = [path.relative_to(root).as_posix() for path in required_paths if not path.is_file()]
    checks.append(DoctorCheck(
        "repository",
        "PASS" if not missing else "FAIL",
        "structure principale présente" if not missing else f"manquants: {', '.join(missing)}",
    ))

    v5_modules = [
        "engineering_memory.py",
        "engineering_reviewer.py",
        "learning_curriculum.py",
        "adaptive_budget.py",
        "confidence_calibration.py",
        "dependency_guard.py",
        "regression_test_guard.py",
        "developer_docs.py",
        "mission_manager.py",
        "meta_learning.py",
        "trusted_git_checkpoint.py",
    ]
    missing_v5 = [name for name in v5_modules if not (root / "self_improvement" / name).is_file()]
    v6_modules = ["sandbox_executor.py", "goal_orchestrator.py", "semantic_memory.py", "memory_facade.py", "consensus_engine.py", "observability.py"]
    v62_modules = ["context_budget.py", "provider_manager.py", "brain_readiness.py"]
    missing_v6 = [name for name in v6_modules if not (root / "self_improvement" / name).is_file()]
    checks.append(DoctorCheck(
        "v5_capabilities",
        "PASS" if not missing_v5 else "WARN",
        "mémoire, reviewer, curriculum, missions, docs, budgets et checkpoints présents"
        if not missing_v5 else f"modules V5 optionnels manquants: {', '.join(missing_v5)}",
    ))

    checks.append(DoctorCheck(
        "v6_capabilities", "PASS" if not missing_v6 else "WARN",
        "sandbox, goal routing, mémoire sémantique, consensus et observabilité présents" if not missing_v6 else f"modules V6 manquants: {', '.join(missing_v6)}",
    ))
    missing_v62 = [name for name in v62_modules if not (root / "self_improvement" / name).is_file()]
    checks.append(DoctorCheck(
        "v6_2_brain", "PASS" if not missing_v62 else "WARN",
        "budget contexte, provider lease manager et brain-check présents" if not missing_v62 else f"modules V6.2 manquants: {', '.join(missing_v62)}",
    ))
    v7_modules = ["omniroute_provider.py", "model_catalog.py", "model_performance.py", "brain_pool.py"]
    missing_v7 = [name for name in v7_modules if not (root / "self_improvement" / name).is_file()]
    checks.append(DoctorCheck(
        "v7_brain", "PASS" if not missing_v7 else "WARN",
        "catalogue dynamique, routage, performance et OmniRoute presents"
        if not missing_v7 else f"modules V7 manquants: {', '.join(missing_v7)}",
    ))

    base_url = os.environ.get("OMNIROUTE_BASE_URL") or os.environ.get("OMNIROUTE_API_BASE") or "http://127.0.0.1:20128/v1"
    checks.append(DoctorCheck(
        "omniroute_configuration", "PASS" if base_url.startswith(("http://127.0.0.1", "http://localhost", "https://")) else "WARN",
        f"base_url configuree: {base_url}",
    ))

    ignore_path = root / ".gitignore"
    ignore_text = ignore_path.read_text(encoding="utf-8", errors="replace") if ignore_path.is_file() else ""
    secret_rules = (".env", "*.key", "*.pem", "credentials", "secrets")
    missing_rules = [rule for rule in secret_rules if rule not in ignore_text]
    checks.append(DoctorCheck(
        "security_config", "PASS" if not missing_rules else "WARN",
        "patterns secrets ignores" if not missing_rules else "patterns .gitignore manquants: " + ", ".join(missing_rules),
    ))

    dependency_names = ("httpx", "pytest")
    optional_dependency_names = ("ollama", "groq", "cerebras", "hypothesis")
    missing_deps = [name for name in dependency_names if importlib.util.find_spec(name) is None]
    missing_optional_deps = [name for name in optional_dependency_names if importlib.util.find_spec(name) is None]
    checks.append(DoctorCheck(
        "dependencies",
        "PASS" if not missing_deps else "FAIL",
        "dépendances critiques importables" if not missing_deps else f"manquantes: {', '.join(missing_deps)}",
    ))
    checks.append(DoctorCheck(
        "optional_provider_dependencies",
        "PASS" if not missing_optional_deps else "WARN",
        "SDK providers optionnels importables" if not missing_optional_deps else f"SDK optionnels manquants: {', '.join(missing_optional_deps)}",
    ))

    recovery_roots = [
        root / ".self_improvement_recovery",
        root / ".self_improvement_supervisor_recovery",
    ]
    pending_recovery = any(
        (recovery / "active.zip").is_file() or (recovery / "active.json").is_file()
        for recovery in recovery_roots
    )
    checks.append(DoctorCheck(
        "recovery",
        "FAIL" if pending_recovery else "PASS",
        "checkpoint interrompu à restaurer" if pending_recovery else "aucun chantier interrompu détecté",
    ))

    writable = os.access(root, os.W_OK)
    checks.append(DoctorCheck(
        "workspace",
        "PASS" if writable else "FAIL",
        "repository accessible en écriture" if writable else "repository non accessible en écriture",
    ))

    git_binary = shutil.which("git")
    if git_binary:
        try:
            proc = subprocess.run(
                [git_binary, "rev-parse", "--is-inside-work-tree"],
                cwd=str(root), capture_output=True, text=True, timeout=3,
            )
            is_repo = proc.returncode == 0 and proc.stdout.strip().casefold() == "true"
        except Exception:
            is_repo = False
        checks.append(DoctorCheck(
            "git",
            "PASS" if is_repo else "WARN",
            "repository Git actif" if is_repo else "Git disponible mais ce dossier n'est pas initialisé",
        ))
    else:
        checks.append(DoctorCheck("git", "WARN", "git introuvable ; rollback interne actif mais historique Git absent"))

    try:
        from self_improvement.sandbox_executor import SandboxExecutor
        sandbox = SandboxExecutor(root)
        docker_ok = sandbox.docker_image_available()
    except Exception:
        docker_ok = False
    checks.append(DoctorCheck(
        "docker_sandbox", "PASS" if docker_ok else "WARN",
        "sandbox Docker V6 prêt" if docker_ok else "Docker/image V6 non prêt; fallback local sanitisé actif",
    ))

    provider_vars = ("CEREBRAS_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "OMNIROUTE_API_KEY")
    configured = [name for name in provider_vars if bool(os.environ.get(name))]
    checks.append(DoctorCheck(
        "remote_providers",
        "PASS" if configured else "WARN",
        ("clés configurées: " + ", ".join(configured)) if configured else "aucune clé distante détectée ; fallback local Ollama possible",
    ))

    failures = [check for check in checks if check.status == "FAIL"]
    overall_status = "NOT_READY" if failures else ("DEGRADED" if any(item.status == "WARN" for item in checks) else "READY")
    return DoctorReport(
        ready=not failures,
        checks=checks,
        details={
            "repo_root": str(root),
            "agent_generation": "V7" if not missing_v7 else ("V6.2" if not missing_v6 and not missing_v62 else ("V6" if not missing_v6 else "V5")),
            "failed_checks": [item.name for item in failures],
            "v5_capabilities_present": not missing_v5,
            "v6_capabilities_present": not missing_v6,
            "docker_sandbox_ready": docker_ok,
            "network_checks_performed": 0,
            "secret_values_exposed": False,
        },
        status=overall_status,
    )
