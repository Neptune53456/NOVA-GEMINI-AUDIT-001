"""Developer Agent V3 : exploration outillée, édition/création multi-fichiers et retry borné."""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from file_editor import generate_patch_edit, validate_python_code
from model_router import MODEL_RUNTIME_TIMEOUT_SECONDS, chat
from self_improvement.failure_analysis import (
    AttemptRecord,
    analyze_failure,
    judge_patch,
    patch_fingerprint,
    plan_retry,
)
from self_improvement.patch_relevance import (
    check_patch_relevance,
    check_patch_relevance_multi,
    required_behavior_contract,
)
from self_improvement.repo_intelligence import RepoIntelligence
from self_improvement.process_safety import sanitized_child_environment
from self_improvement.sandbox_executor import SandboxExecutor, SandboxLimits
from self_improvement.developer_tools import ReadOnlyDeveloperExplorer, DeveloperExplorationResult
from self_improvement.agent_path_policy import is_agent_editable_path
from self_improvement.agent_code_safety import introduced_restricted_capabilities
from self_improvement.engineering_reviewer import EngineeringReviewer, ReviewResult
from self_improvement.confidence_calibration import calibrate_developer_confidence
from self_improvement.dependency_guard import DependencyGuard
from self_improvement.regression_test_guard import RegressionTestGuard, RegressionProbeResult
from self_improvement.developer_strategy import short_plan_prompt, new_file_prompt
from self_improvement.candidate_evidence import changed_python_symbols
from self_improvement.test_failure_summary import (
    RepairProgress,
    TestFailureSummary,
    build_repair_instruction,
    compare_failures,
    parse_test_failure,
)
from self_improvement.patch_failure_evidence import classify_patch_failure
from self_improvement.public_workspace_resources import select_worker_safe_tests
from self_improvement.root_cause_evidence import build_root_cause_evidence


EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".self_improvement_holdout",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".hypothesis",
    ".temp_tests",
    ".self_improvement_worktrees",
    ".self_improvement_discoveries",
    "benchmark_results",
}


PROBABLE_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b", re.IGNORECASE),
    re.compile(r"\bgsk_[A-Za-z0-9_-]{20,}\b", re.IGNORECASE),
    re.compile(r"\bcsk-[A-Za-z0-9_-]{20,}\b", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"(?:api[_-]?key|password|secret|token)\s*[:=]\s*['\"][^'\"]{12,}['\"]", re.IGNORECASE),
)


@dataclass
class DeveloperTask:
    task: str
    relevance_task: str | None = None
    constraints: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    max_iterations: int = 2
    dry_run: bool = False
    require_regression_proof: bool = False
    docs_domains: list[str] = field(default_factory=list)
    model_budget: Any = None
    reviewer_model_budget: Any = None
    candidate_first: bool = False
    deterministic_handoff: bool = False
    max_source_files: int = 5
    max_diff_lines: int = 300
    execution_limits: dict | None = None
    grounded_symbol: str = ""
    expected_behavior: str = ""
    observed_behavior: str = ""
    prior_failures: list[str] = field(default_factory=list)


@dataclass
class DeveloperResult:
    success: bool
    task: str
    plan: str
    files_changed: list[str]
    iterations: int
    tests_run: list[str]
    tests_passed: bool
    failure_reason: str | None
    diff_summary: str
    tests_requested: list[str] = field(default_factory=list)
    files_targeted: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    attempt_history: list[dict] = field(default_factory=list)
    dry_run: bool = False
    auto_commit: bool = False
    auto_push: bool = False
    model_used: str | None = None
    model_attempts: int | None = None
    logical_model_requests: int | None = None
    model_duration_ms: int | None = None
    files_created: list[str] = field(default_factory=list)
    tests_inferred: list[str] = field(default_factory=list)
    replan_requested: bool = False
    recommended_files: list[str] = field(default_factory=list)
    recommended_tests: list[str] = field(default_factory=list)
    exploration_summary: str = ""
    tool_trace: list[dict] = field(default_factory=list)
    tool_calls: int = 0
    rollback_performed: bool = False
    exploration_model_turns: int = 0
    review_decision: str | None = None
    reviewer_confidence: float = 0.0
    reviewer_summary: str = ""
    reviewer_concerns: list[str] = field(default_factory=list)
    confidence_score: float = 0.0
    confidence_band: str = "low"
    regression_probe_status: str = "not_run"
    dependency_findings: list[str] = field(default_factory=list)
    status: str = "unknown"
    summary: str = ""
    files_examined: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tests_added: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    needs_replan: bool = False
    replan_reason: str | None = None
    confidence: float = 0.0
    pipeline_trace: dict[str, object] = field(default_factory=dict)
    repair_attempted: bool = False
    repair_count: int = 0
    failure_before_repair: dict | None = None
    result_after_repair: dict | None = None


class DeveloperAgent:
    """PETIT agent de développement avec boucle bornée et protections de chemin."""

    def __init__(
        self,
        *,
        repo_root: str | Path | None = None,
        chat_function: Callable | None = None,
        patch_generator: Callable | None = None,
        new_file_generator: Callable | None = None,
        test_runner: Callable | None = None,
        max_files: int = 5,
        repo_intelligence: RepoIntelligence | None = None,
        developer_explorer: ReadOnlyDeveloperExplorer | None = None,
        enable_tool_loop: bool = True,
        max_tool_steps: int = 3,
        test_timeout_seconds: float = 120.0,
        lint_timeout_seconds: float = 30.0,
        reviewer: EngineeringReviewer | None = None,
        enable_review: bool = True,
        reviewer_threshold: float = 0.68,
        dependency_guard: DependencyGuard | None = None,
        regression_test_guard: RegressionTestGuard | None = None,
    ):
        self.repo_root = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[1]
        self._chat_injected = chat_function is not None
        self.chat_function = chat_function or chat
        self.patch_generator = patch_generator
        self.new_file_generator = new_file_generator
        self._uses_default_test_runner = test_runner is None
        self.test_runner = test_runner or self._default_test_runner
        self.max_files = max_files
        self.repo_intelligence = repo_intelligence or RepoIntelligence(self.repo_root)
        self.enable_tool_loop = bool(enable_tool_loop)
        self.max_tool_steps = max(1, min(int(max_tool_steps), 16))
        self.test_timeout_seconds = max(5.0, min(float(test_timeout_seconds), 900.0))
        self.lint_timeout_seconds = max(2.0, min(float(lint_timeout_seconds), 120.0))
        self.developer_explorer = developer_explorer
        self.reviewer = reviewer
        self.enable_review = bool(enable_review)
        self.reviewer_threshold = max(0.5, min(float(reviewer_threshold), 0.95))
        self.dependency_guard = dependency_guard or DependencyGuard(self.repo_root)
        self.regression_test_guard = regression_test_guard or RegressionTestGuard(self.repo_root)
        self._active_model_budget = None
        self._reviewer_model_budget = None

    def _resolve_repo_path(self, raw_path: str | Path) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = (self.repo_root / candidate).resolve(strict=False)
        else:
            candidate = candidate.resolve(strict=False)
        candidate.relative_to(self.repo_root)
        relative = candidate.relative_to(self.repo_root)
        relative_parts = {part.casefold() for part in relative.parts}
        if relative_parts & {name.casefold() for name in EXCLUDED_DIRECTORY_NAMES}:
            raise ValueError(f"Chemin exclu du périmètre : {candidate}")
        if not is_agent_editable_path(relative):
            raise ValueError(f"Chemin privé ou type de fichier non autorisé : {candidate}")
        return candidate

    def _read_repo_file(self, raw_path: str | Path) -> str:
        resolved = self._resolve_repo_path(raw_path)
        if not resolved.is_file():
            raise FileNotFoundError(f"Fichier introuvable : {resolved}")
        return resolved.read_text(encoding="utf-8", errors="replace")

    @staticmethod
    def _contains_probable_secret(content: str) -> bool:
        return any(pattern.search(content or "") for pattern in PROBABLE_SECRET_PATTERNS)

    def _is_repo_allowed(self, raw_path: str) -> bool:
        try:
            self._resolve_repo_path(raw_path)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _safe_files(self, candidate_paths: Iterable[str], *, allow_missing: bool = False) -> list[str]:
        """Résout des chemins autorisés. Les chemins manquants ne sont permis que sur demande explicite."""
        allowed: list[str] = []
        for raw in candidate_paths:
            if not raw or not isinstance(raw, str):
                continue
            if self._is_repo_allowed(raw):
                resolved = self._resolve_repo_path(raw)
                if (resolved.is_file() or allow_missing) and resolved.is_relative_to(self.repo_root):
                    allowed.append(str(resolved))
        return list(dict.fromkeys(allowed))[: self.max_files]

    def _infer_target_files(self, task: str, requested: list[str], tests: list[str] | None = None) -> list[str]:
        """Sélectionne les fichiers à modifier et autorise explicitement la création de nouveaux fichiers."""
        if requested:
            # Les créations sont autorisées uniquement lorsqu'elles sont explicitement
            # listées dans target_files. Le Planner V2 y ajoute les nouveaux tests.
            return self._safe_files(requested, allow_missing=True)

        # Sans cibles explicites, Repo Intelligence donne une sélection structurée et existante.
        relevant = self.repo_intelligence.relevant_files(task, limit=self.max_files)
        selected = [str((self.repo_root / info.path).resolve()) for info in relevant]
        if selected:
            return selected[: self.max_files]

        # Fallback très conservateur : fichiers Python publics existants uniquement.
        return [
            str((self.repo_root / path).resolve())
            for path in self.repo_intelligence.iter_files(suffixes={".py"})[: self.max_files]
        ]

    def _infer_tests(self, files: list[str]) -> list[str]:
        rels: list[str] = []
        for file_path in files:
            try:
                rels.append(Path(file_path).resolve().relative_to(self.repo_root).as_posix())
            except ValueError:
                continue
        return self.repo_intelligence.find_tests_for(rels, limit=8)

    def _safe_test_paths(self, tests: Iterable[str]) -> list[str]:
        """Valide strictement les tests avant toute exécution subprocess.

        Même un appel direct au DeveloperAgent (hors Planner) ne peut donc pas
        transformer `pytest <chemin>` en primitive d'exécution hors repository.
        """
        safe: list[str] = []
        for raw in list(tests)[:16]:
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                resolved = self._resolve_repo_path(raw)
                rel = resolved.relative_to(self.repo_root).as_posix()
            except (OSError, TypeError, ValueError):
                continue
            name = resolved.name.casefold()
            # Un test peut être un nouveau fichier approuvé qui n'existe pas encore
            # au moment du preflight. Le chemin est déjà borné au repository ; s'il
            # reste absent après écriture, pytest échouera proprement au lieu de
            # transformer cela en faux succès.
            if resolved.suffix.casefold() != ".py":
                continue
            if not (name.startswith("test_") or any(part.casefold() in {"test", "tests"} for part in resolved.relative_to(self.repo_root).parts[:-1])):
                continue
            if rel not in safe:
                safe.append(rel)
        return safe[:12]

    def _explore_before_edit(
        self,
        task: str,
        files: list[str],
        tests: list[str],
        docs_domains: list[str] | None = None,
        max_steps: int | None = None,
    ) -> DeveloperExplorationResult | None:
        """Lance une boucle read-only uniquement avec les générateurs de production.

        Les tests et intégrations qui injectent un patch_generator gardent ainsi le
        contrat historique déterministe. En production, l'agent observe le repo avant
        de demander le premier patch.
        """
        if not self.enable_tool_loop:
            return None
        # Un explorer injecté explicitement reste actif même avec des générateurs
        # de patch injectés : cela rend la boucle agentique testable sans réseau.
        if self.developer_explorer is None and (self.patch_generator is not None or self.new_file_generator is not None):
            return None
        explorer = self.developer_explorer or ReadOnlyDeveloperExplorer(
            self.repo_root,
            chat_function=self.chat_function,
            repo_intelligence=self.repo_intelligence,
            max_steps=(
                max(1, min(self.max_tool_steps, int(max_steps)))
                if max_steps is not None else self.max_tool_steps
            ),
            docs_domains=list(docs_domains or []),
            model_budget=self._active_model_budget,
        )
        return explorer.explore(task, approved_files=files, requested_tests=tests)

    def _diagnose_after_failure(
        self,
        task: str,
        files: list[str],
        tests: list[str],
        failure: str,
        docs_domains: list[str] | None = None,
    ) -> DeveloperExplorationResult | None:
        """Ré-explore le repo après un échec réel avant de retenter un patch.

        Cette phase reste strictement read-only. Elle peut enrichir le retry ou
        demander une replanification si l'échec révèle que le périmètre d'écriture
        approuvé est insuffisant.
        """
        if not self.enable_tool_loop:
            return None
        if self.developer_explorer is None and (self.patch_generator is not None or self.new_file_generator is not None):
            return None
        explorer = self.developer_explorer or ReadOnlyDeveloperExplorer(
            self.repo_root,
            chat_function=self.chat_function,
            repo_intelligence=self.repo_intelligence,
            max_steps=min(5, self.max_tool_steps),
            docs_domains=list(docs_domains or []),
            model_budget=self._active_model_budget,
        )
        diagnostic_task = (
            f"{task}\n\nÉCHEC OBSERVÉ APRÈS MODIFICATION/TEST :\n"
            f"{str(failure)[:2500]}\n\n"
            "Diagnostique la cause à partir du repository réel. Si les fichiers "
            "approuvés ne suffisent pas, demande replan au lieu d'inventer un patch."
        )
        return explorer.explore(
            diagnostic_task,
            approved_files=files,
            requested_tests=tests,
        )

    def _review_after_tests(
        self,
        task: str,
        candidates: list[tuple[str, str, str]],
        tests_run: list[str],
        tests_passed: bool,
    ) -> ReviewResult | None:
        """Critique indépendante non autoritaire après les tests ciblés.

        Les générateurs injectés dans les tests conservent le comportement historique
        sauf si un reviewer est lui-même explicitement injecté.
        """
        if not self.enable_review:
            return None
        if self.reviewer is None and (self.patch_generator is not None or self.new_file_generator is not None):
            return None
        reviewer = self.reviewer or EngineeringReviewer(
            self.repo_root, chat_function=self.chat_function, repo_intelligence=self.repo_intelligence
        )
        return reviewer.review(
            task=task,
            changes=candidates,
            tests_run=list(tests_run),
            tests_passed=bool(tests_passed),
            model_budget=self._reviewer_model_budget or self._active_model_budget,
        )

    def _llm_plan(self, task: str, files: list[str]) -> tuple[str, str | None, int | None, int | None]:
        if self.patch_generator is not None and not self._chat_injected:
            return task, None, 0, 0
        schema = {
            "type": "object",
            "properties": {
                "plan": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["plan", "files"],
        }
        started = time.perf_counter()
        print("[Developer] Analyse de la tâche...")
        try:
            budget_kwargs = {"model_budget": self._active_model_budget} if self._active_model_budget is not None else {}
            response = self.chat_function(
                messages=[{"role": "user", "content": short_plan_prompt(task=task, files=files)}],
                task_type="developer_plan",
                format=schema,
                options={"temperature": 0},
                think=False,
                **budget_kwargs,
            )
            payload = response.get("message", {}).get("content", "")
            if isinstance(payload, str):
                parsed = json.loads(payload)
            else:
                parsed = payload
            plan = str(parsed.get("plan") or "Plan généré.")
            model_name = response.get("_meta", {}).get("model") or "inconnu"
            model_attempts = response.get("_meta", {}).get("attempts")
            model_duration_ms = response.get("_meta", {}).get("duration_ms")
            took_ms = int((time.perf_counter() - started) * 1000)
            print(f"[Developer] Appel modèle: {model_name}")
            print(f"[Developer] Timeout: {MODEL_RUNTIME_TIMEOUT_SECONDS}s")
            print(f"[Developer] Réponse reçue en {took_ms / 1000:.1f}s")
            return plan, model_name, model_attempts, model_duration_ms or took_ms
        except Exception:
            print(f"[Developer] Appel modèle: fallback local")
            return "1. Identifier les fichiers concernés. 2. Appliquer une correction ciblée. 3. Valider syntaxe et tests ciblés.", None, None, None

    def _is_ambiguous_patch_error(self, message: str | None) -> bool:
        if not message:
            return False
        text = message.lower()
        return (
            "apparaît" in text and "fois" in text and "ambigu" in text
        ) or "multiple matches" in text or "appears" in text and "times" in text and "ambiguous" in text

    def _is_structured_patch_error(self, message: str | None) -> bool:
        if not message:
            return False
        text = message.lower()
        return any(code in text for code in (
            "n'existe pas dans le fichier",
            "anchor_not_found",
            "anchor_ambiguous",
            "symbol_not_found",
            "symbol_ambiguous",
            "old_text manquant",
            "symbol manquant",
            "anchor manquante",
            "invalid_patch_response",
            "invalid_patch_generator_contract",
        )) or ("old_text" in text and ("vide" in text or "manquant" in text))

    def _extract_ambiguous_context(self, content: str, limit_lines: int = 20) -> str:
        lines = content.splitlines()
        if not lines:
            return ""
        snippet = lines[:limit_lines]
        if len(lines) > limit_lines:
            snippet = lines[:limit_lines]
        return "\n".join(snippet)

    def _build_precision_retry_instruction(self, file_path: str, error: str, candidate: str | None = None, count: int | None = None) -> str:
        context = []
        context.append("Fichier concerné: " + file_path)
        if candidate:
            context.append("Ancien extrait ambigu: " + candidate[:800])
        if count is not None:
            context.append(f"Occurrences trouvées: {count}")
        context.append("Priorité: replace_symbol_block pour une fonction/classe entière (fournir 'symbol' et 'new_text').")
        context.append("Pour une petite insertion, utilise insert_after_anchor ou insert_before_anchor avec une ancre exacte ('anchor' et 'new_text').")
        context.append("N'utilise replace qu'avec un old_text court et unique ('old_text' et 'new_text').")
        context.append("Régénère le patch avec un format JSON strict respectant les champs obligatoires selon l'action choisie.")
        context.append("Régénère le patch avec davantage de contexte unique.")
        context.append("Si une opération replace est incomplète, old_text est absent : préfère replace_symbol_block quand un symbole Python est identifiable.")
        return "\n".join(context) + "\nErreur: " + error

    def _default_patch_generator(self, file_path: str, instruction: str):
        budget_kwargs = (
            {"model_budget": self._active_model_budget}
            if self._active_model_budget is not None
            else {}
        )
        return generate_patch_edit(
            file_path,
            instruction,
            task_type="developer_patch",
            project_root=self.repo_root,
            **budget_kwargs,
        )

    def _default_new_file_generator(self, file_path: str | Path, instruction: str):
        """Génère le contenu COMPLET d'un fichier inexistant via le routeur existant."""
        resolved = self._resolve_repo_path(file_path)
        relative = resolved.relative_to(self.repo_root).as_posix()
        context = self.repo_intelligence.context_for_objective(
            f"{instruction}\nFichier demandé: {relative}",
            max_files=3,
            max_chars_per_file=900,
            include_inventory=12,
        )
        schema = {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["content", "summary"],
        }
        prompt = new_file_prompt(
            relative_path=relative,
            instruction=instruction,
            repo_context=context,
        )
        messages = [{"role": "user", "content": prompt}]
        last_error: ValueError | None = None
        parsed: dict | None = None
        # Un unique retry est autorisé pour une réponse transport valide mais
        # incompatible avec le contrat métier. Cela couvre un backend qui ignore
        # le JSON Schema sans transformer une indisponibilité en boucle d'appels.
        for schema_attempt in range(2):
            try:
                budget_kwargs = {"model_budget": self._active_model_budget} if self._active_model_budget is not None else {}
                response = self.chat_function(
                    messages=messages,
                    task_type="developer_patch",
                    format=schema,
                    options={"temperature": 0},
                    think=False,
                    **budget_kwargs,
                )
            except Exception as exc:
                if last_error is not None:
                    raise last_error from exc
                raise RuntimeError(
                    "new_file_generation_failed: "
                    f"file='{relative}'; cause={type(exc).__name__}"
                ) from exc
            payload = response.get("message", {}).get("content", "") if isinstance(response, dict) else ""
            try:
                parsed = self._parse_new_file_response(payload, relative)
                break
            except ValueError as exc:
                last_error = exc
                if schema_attempt:
                    raise
                messages = [
                    *messages,
                    {"role": "assistant", "content": self._safe_schema_mismatch_excerpt(payload)},
                    {
                        "role": "user",
                        "content": (
                            "La réponse précédente ne respecte pas NewFileResponse. "
                            "Réessaie une seule fois. Retourne uniquement un objet JSON "
                            "avec exactement les champs obligatoires `content` (chaîne non vide, "
                            "contenu complet du fichier) et `summary` (chaîne)."
                        ),
                    },
                ]
        if parsed is None:  # pragma: no cover - garde défensive
            raise last_error or ValueError("invalid_new_file_response")
        content = parsed["content"]
        summary = parsed.get("summary")
        if not isinstance(summary, str):
            summary = "Nouveau fichier généré."
        return "", content, summary

    @staticmethod
    def _safe_schema_mismatch_excerpt(payload, limit: int = 600) -> str:
        """Rend le mauvais payload utile au retry sans recopier un gros contenu."""
        if isinstance(payload, dict):
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)[:limit]
        if isinstance(payload, str):
            return payload[:limit]
        return f"<{type(payload).__name__}>"

    @staticmethod
    def _parse_new_file_response(payload, relative_path: str) -> dict:
        """Normalise les enveloppes de transport puis valide le contrat métier."""
        label = str(relative_path).replace("\n", " ").replace("\r", " ")
        parsed = payload
        if isinstance(payload, str):
            candidate = payload.strip()
            if not candidate:
                raise ValueError(
                    f"invalid_new_file_response: fichier '{label}': message.content est vide; "
                    "objet JSON avec champ 'content' requis."
                )
            fence = re.fullmatch(
                r"```(?:json)?\s*\r?\n?(.*?)\r?\n?```",
                candidate,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if fence:
                candidate = fence.group(1).strip()
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid_new_file_response: fichier '{label}': message.content doit contenir "
                    f"un objet JSON valide ({exc.msg}, ligne {exc.lineno}, colonne {exc.colno})."
                ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"invalid_new_file_response: fichier '{label}': structure "
                f"{type(parsed).__name__} invalide; objet JSON avec champ 'content' requis."
            )
        if "content" not in parsed:
            fields = sorted(str(key) for key in parsed.keys())
            raise ValueError(
                f"invalid_new_file_response: fichier '{label}': champ 'content' manquant "
                f"dans l'objet JSON (champs reçus: {fields})."
            )
        content = parsed["content"]
        if not isinstance(content, str):
            raise ValueError(
                f"invalid_new_file_response: fichier '{label}': champ 'content' doit être une chaîne, "
                f"pas {type(content).__name__}."
            )
        if not content.strip():
            raise ValueError(
                f"invalid_new_file_response: fichier '{label}': champ 'content' est vide."
            )
        # Certains backends respectent le schéma JSON mais placent encore le
        # fichier lui-même dans une unique fence Markdown. Cette enveloppe de
        # présentation n'est pas du code et peut être retirée sans assouplir la
        # validation : seules les fences fermées couvrant tout le contenu sont
        # acceptées, puis les gardes syntaxe/ruff s'appliquent normalement.
        content_fence = re.fullmatch(
            r"```(?:[A-Za-z0-9_+.-]+)?\s*\r?\n(.*?)\r?\n```\s*",
            content.strip(),
            flags=re.DOTALL,
        )
        if content_fence:
            normalized = content_fence.group(1)
            parsed = dict(parsed)
            parsed["content"] = normalized + ("\n" if content.endswith("\n") else "")
        return parsed

    def _default_test_runner(self, tests: list[str], root: Path):
        if not tests:
            return {
                "passed": False,
                "returncode": None,
                "command": [],
                "output_tail": "Aucun test demandé; aucun test exécuté.",
                "tests_run": [],
            }
        # Stop on the first public-test failure. A candidate can only pass if
        # the whole selection is green, while fail-fast preserves the concrete
        # traceback needed by the single bounded semantic-repair attempt and
        # avoids burning the timeout after failure is already established.
        command = [sys.executable, "-m", "pytest", *tests, "--no-cov", "-q"]
        if not getattr(self, "_baseline_preflight", False):
            command.append("--maxfail=1")
        executor = SandboxExecutor(
            root,
            mode=os.getenv("PROJET_IA_SANDBOX", "auto"),
            limits=SandboxLimits(timeout_seconds=self.test_timeout_seconds),
        )
        completed = executor.run(command)
        if completed.timed_out:
            return {
                "passed": False, "returncode": None, "command": command,
                "output_tail": f"test_timeout_after_{self.test_timeout_seconds:.0f}s\n{completed.stdout}\n{completed.stderr}"[-4000:],
                "tests_run": list(tests), "sandbox_backend": completed.backend,
            }
        return {
            "passed": completed.returncode == 0,
            "returncode": completed.returncode,
            "command": command,
            "sandbox_backend": completed.backend,
            "output_tail": ((completed.stdout or "") + "\n" + (completed.stderr or ""))[-4000:],
            "tests_run": list(tests),
        }

    @staticmethod
    def _unpack_patch(patched):
        if not isinstance(patched, (tuple, list)) or len(patched) != 3:
            raise ValueError(
                "invalid_patch_generator_contract: attendu (original, new_content, summary)."
            )
        original, new_content, summary = patched
        if not isinstance(original, str) or not isinstance(new_content, str) or not isinstance(summary, str):
            raise ValueError(
                "invalid_patch_generator_contract: original, new_content et summary doivent être des chaînes."
            )
        return original, new_content, summary

    @staticmethod
    def _diff_summary(file_path: str, original: str, new_content: str) -> str:
        diff = list(difflib.ndiff(original.splitlines(), new_content.splitlines()))
        added = sum(line.startswith("+ ") for line in diff)
        removed = sum(line.startswith("- ") for line in diff)
        return f"{Path(file_path).name}: +{added}/-{removed} lignes"

    def _compile_python_files(self, file_paths: list[str]) -> tuple[bool, str | None]:
        for file_path in file_paths:
            try:
                path = Path(file_path)
                source = path.read_text(encoding="utf-8", errors="replace")
                compile(source, str(path), "exec")
            except Exception as exc:  # pragma: no cover - defensive
                return False, str(exc)
        return True, None

    def _compile_candidate_contents(
        self, candidates: list[tuple[str, str, str]]
    ) -> tuple[bool, str | None]:
        """Valide la syntaxe de tous les candidats en mémoire (avant écriture)."""
        for file_path, _original, new_content in candidates:
            if not file_path.endswith(".py"):
                continue
            try:
                compile(new_content, file_path, "exec")
            except SyntaxError as exc:
                return False, f"{Path(file_path).name}: {exc}"
        return True, None

    def _candidate_interface_context(
        self, candidates: list[tuple[str, str, str]], *, max_chars: int = 4000
    ) -> str:
        """Résume les interfaces Python déjà validées sans recopier leur contenu."""
        summaries: list[str] = []
        for file_path, _original, source in candidates[-8:]:
            try:
                relative = Path(file_path).resolve().relative_to(self.repo_root).as_posix()
            except ValueError:
                relative = Path(file_path).name
            module = relative[:-3].replace("/", ".") if relative.endswith(".py") else relative
            if module.endswith(".__init__"):
                module = module[:-9]
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            symbols: list[str] = []
            imports: list[str] = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    symbols.append(f"{prefix} {node.name}{ast.unparse(node.args)}")
                elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                    bases = ", ".join(ast.unparse(base) for base in node.bases)
                    symbols.append(f"class {node.name}" + (f"({bases})" if bases else ""))
                elif isinstance(node, (ast.Import, ast.ImportFrom)) and len(imports) < 8:
                    imports.append(ast.unparse(node))
            rendered = (
                f"path={relative}\nmodule={module}\n"
                f"public={symbols[:16]}\nimports={imports}"
            )
            summaries.append(rendered)
        return "\n---\n".join(summaries)[:max_chars]

    def _validate_intra_batch_imports(
        self, candidates: list[tuple[str, str, str]]
    ) -> tuple[bool, str | None]:
        """Détecte un module inventé quand ses symboles viennent clairement du lot."""
        modules: dict[str, tuple[str, set[str]]] = {}
        parsed: list[tuple[str, ast.Module]] = []
        for file_path, _original, source in candidates:
            if not file_path.endswith(".py"):
                continue
            try:
                relative = Path(file_path).resolve().relative_to(self.repo_root).as_posix()
                tree = ast.parse(source)
            except (ValueError, SyntaxError):
                continue
            module = relative[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            public = {
                node.name for node in tree.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and not node.name.startswith("_")
            }
            modules[module] = (relative, public)
            parsed.append((relative, tree))

        for relative, tree in parsed:
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module or node.level:
                    continue
                imported_module = node.module
                if imported_module in modules:
                    continue
                module_path = self.repo_root / (imported_module.replace(".", "/") + ".py")
                package_path = self.repo_root / imported_module.replace(".", "/") / "__init__.py"
                if module_path.is_file() or package_path.is_file():
                    continue
                names = {alias.name for alias in node.names if alias.name != "*"}
                matches = [
                    module for module, (_path, public) in modules.items()
                    if names and names.issubset(public)
                ]
                if len(matches) == 1:
                    return False, (
                        "INTRA_BATCH_IMPORT_VALIDATION: "
                        f"{relative} importe le module inexistant '{imported_module}' "
                        f"pour {sorted(names)}, fournis par le candidat '{matches[0]}'."
                    )
        return True, None

    @staticmethod
    def _validate_generated_test_discovery(
        candidates: list[tuple[str, str, str]], _requested_tests: list[str]
    ) -> tuple[bool, str | None]:
        for file_path, _original, source in candidates:
            path = Path(file_path)
            if not path.name.startswith("test_"):
                continue
            if path.suffix.casefold() != ".py":
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            discoverable = any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
                for node in tree.body
            )
            if not discoverable:
                discoverable = any(
                    isinstance(node, ast.ClassDef)
                    and node.name.startswith("Test")
                    and any(
                        isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and member.name.startswith("test_")
                        for member in node.body
                    )
                    for node in tree.body
                )
            if not discoverable:
                return False, (
                    "generated_test_not_discoverable: "
                    f"{path.name} ne définit aucune fonction test_* ni méthode test_* dans une classe Test*."
                )
        return True, None

    def _run_ruff_check(self, file_paths: list[str]) -> tuple[bool, str | None]:
        """Compatibilité : lint les fichiers actuellement présents."""
        candidates: list[tuple[str, str, str]] = []
        for raw in file_paths:
            path = Path(raw)
            if path.suffix.casefold() != ".py" or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            candidates.append((str(path), content, content))
        return self._run_ruff_check_candidates(candidates)

    def _run_ruff_check_candidates(
        self,
        candidates: list[tuple[str, str, str]],
    ) -> tuple[bool, str | None]:
        """Lint réellement le contenu candidat sans l'écrire sur disque.

        `ruff --stdin-filename` conserve le nom logique du fichier pour la config
        tout en analysant exactement `new_content`. Le garde reste optionnel si
        Ruff n'est pas installé, mais il est borné dans le temps lorsqu'il l'est.
        """
        import shutil

        if not shutil.which("ruff"):
            return True, None
        for file_path, _original, new_content in candidates:
            if Path(file_path).suffix.casefold() != ".py":
                continue
            try:
                result = subprocess.run(
                    ["ruff", "check", "--stdin-filename", str(file_path), "-"],
                    input=new_content,
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=self.lint_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return False, f"quality_gate_timeout: ruff > {self.lint_timeout_seconds:.0f}s"
            if result.returncode != 0:
                output = ((result.stdout or "") + "\n" + (result.stderr or ""))[-3000:]
                return False, f"quality_gate_failure (ruff candidate): {output}"
        return True, None

    def _run_tests(self, tests: list[str], root: Path):
        limits = getattr(self, "_execution_limits", None)
        if limits is not None and tests:
            limits.phase_timeout(self.test_timeout_seconds, minimum=self.test_timeout_seconds, future_seconds=420.0)
        if not tests:
            return self.test_runner([], root)
        return self.test_runner(tests, root)

    def _apply_patch(self, path: str, original: str, new_content: str):
        resolved = self._resolve_repo_path(path)
        existed = resolved.is_file()
        current = resolved.read_text(encoding="utf-8", errors="replace") if existed else ""
        if original != current:
            raise ValueError("patch_provenance_mismatch: le contenu source du patch diffère du contenu courant; modification refusée.")
        if original == new_content:
            return False
        is_valid, error = validate_python_code(str(resolved), new_content)
        if not is_valid:
            raise ValueError(f"Le nouveau code Python contient une erreur de syntaxe : {error}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            dir=resolved.parent,
            delete=False,
            encoding="utf-8",
            newline="",
        ) as temp_file:
            temp_file.write(new_content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        try:
            os.replace(temp_path, resolved)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
        return True

    def run(self, task: DeveloperTask) -> DeveloperResult:
        self._execution_limits = None
        if task.execution_limits is not None:
            from self_improvement.execution_limits import ExecutionLimits
            self._execution_limits = ExecutionLimits.from_dict(task.execution_limits)
            if (task.max_source_files, task.max_diff_lines) != (self._execution_limits.max_source_files, self._execution_limits.max_diff_lines):
                raise ValueError("unsupported_execution_limits: developer caps differ")
            self._execution_limits.phase_timeout(120, minimum=120, future_seconds=420)
        self._active_model_budget = task.model_budget
        usage_root = task.model_budget
        while usage_root is not None and getattr(usage_root, "parent", None) is not None:
            usage_root = usage_root.parent
        initial_provider_attempts = usage_root.used_calls if usage_root is not None else 0
        initial_logical_requests = usage_root.logical_requests if usage_root is not None else 0
        if task.candidate_first and callable(getattr(task.model_budget, "child", None)):
            # Post-replan patch generation is a bounded candidate-production
            # envelope. Provider fallback remains possible inside these four
            # calls, while a flat patch loop cannot consume the whole campaign.
            self._active_model_budget = task.model_budget.child(8)
        self._reviewer_model_budget = task.reviewer_model_budget
        task_name = task.task
        relevance_task = task.relevance_task or task_name
        files = self._infer_target_files(task_name, task.target_files, task.tests)
        if task.candidate_first and len(files) > 1:
            # A post-replan task must produce one testable candidate before it
            # fans out across a broad plan. Prefer the source target explicitly
            # named by the task; stable planner order is the final tie-breaker.
            folded_task = task_name.casefold()
            ranked = sorted(
                enumerate(files),
                key=lambda item: (
                    -int(Path(item[1]).name.casefold() in folded_task),
                    -int(Path(item[1]).stem.casefold() in folded_task),
                    int(Path(item[1]).name.casefold().startswith("test_")),
                    item[0],
                ),
            )
            files = [ranked[0][1]]
        plan = "Aucun fichier cible valide n'a été trouvé dans le dépôt."
        model_used = model_attempts = model_duration_ms = None
        changed_files: list[str] = []
        written_files: list[str] = []
        created_files: list[str] = []
        summaries: list[str] = []
        history: list[dict] = []
        failed_fingerprints: set[str] = set()
        failed_content_fingerprints: set[str] = set()
        exploration: DeveloperExplorationResult | None = None
        replan_requested = False
        recommended_files: list[str] = []
        recommended_tests: list[str] = []
        diagnostic_explorations: list[DeveloperExplorationResult] = []
        rollback_performed = False
        generation_calls = 0
        repair_attempted = False
        relevance_repair_attempted = False
        target_repair_attempted = False
        repair_count = 0
        failure_before_repair: TestFailureSummary | None = None
        result_after_repair: dict | None = None
        previous_candidates: list[tuple[str, str, str]] = []
        review_result: ReviewResult | None = None
        regression_probe: RegressionProbeResult | None = None
        dependency_findings: list[str] = []
        pipeline_trace: dict[str, bool | str | None] = {
            "developer_reached": True,
            "developer_response_received": False,
            "patch_proposal_parsed": False,
            "patch_protocol_accepted": False,
            "patch_applied": False,
            "syntax_passed": False,
            "public_tests_selected": False,
            "public_tests_executed": False,
            "public_tests_passed": False,
            "reviewer_reached": False,
            "developer_judge_reached": False,
            "candidate_ready": False,
            "duplicate_patch": False,
            "deterministic_diagnostic_used": False,
            "no_progress": False,
            "handoff_reason": None,
            "patch_initial": None,
            "test_failure_summary": None,
            "semantic_repair": False,
            "repair_delta": None,
            "repaired_tests": None,
            "patch_failure_evidence": [],
            "patch_attempts": [],
            "terminal_reason": None,
            "pretest_patch_repair": False,
            "relevance_repair": False,
            "target_repair": False,
            "requirement_coverage_before": None,
            "requirement_coverage_after": None,
            "root_cause_evidence": None,
            "strategy_escalation": None,
        }

        def record_patch_attempt(
            *,
            attempt_id: int,
            file_path: str | None,
            patch_trace: dict | None,
            error: BaseException | None,
            evidence: dict,
        ) -> None:
            """Keep a sanitized, per-attempt explanation of patch disposition."""
            trace = dict(patch_trace or {})
            protocol_attempts = list(trace.get("patch_attempts") or [])
            operation = protocol_attempts[-1] if protocol_attempts else {}
            provider = getattr(error, "provider", None) or trace.get("provider")
            model = getattr(error, "model", None) or trace.get("model")
            raw_material = trace.get("raw_model_output") or repr(error) or repr(trace)
            attempt = {
                "attempt_id": attempt_id,
                "provider": provider,
                "model": model,
                "target_id": operation.get("target_id", "T1"),
                "canonical_file": Path(file_path).name if file_path else None,
                "symbol": (operation.get("source_blocks") or [{}])[0].get("symbol")
                if operation.get("source_blocks") else None,
                "raw_model_output_hash": hashlib.sha256(
                    str(raw_material).encode("utf-8", errors="replace")
                ).hexdigest(),
                "patch_proposal_parse": trace.get("parse_result", "failed" if error else "passed"),
                "schema_error": evidence.get("deterministic_message") if evidence.get("category") == "PATCH_SCHEMA_FAILURE" else None,
                "target_validation": "failed" if evidence.get("category") == "PATCH_TARGET_FAILURE" else trace.get("target_validation", "not_run"),
                "scope_validation": "failed" if evidence.get("category") == "PATCH_SCOPE_FAILURE" else trace.get("scope_validation", "not_run"),
                "apply_result": trace.get("apply_result", "failed" if error else "not_run"),
                "syntax_result": trace.get("syntax_result", "failed" if evidence.get("category") == "PATCH_SYNTAX_FAILURE" else "not_run"),
                "ast_result": trace.get("ast_result", "not_run"),
                "repair_triggered": bool(trace.get("pretest_repair_used") or trace.get("repair_attempted")),
                "repair_result": "no_progress" if trace.get("no_progress") else ("passed" if trace.get("pretest_repair_used") else "not_run"),
                "no_progress_fingerprint": evidence.get("previous_patch_fingerprint") if trace.get("no_progress") else None,
                "terminal_reason": evidence.get("category"),
            }
            pipeline_trace["patch_attempts"].append(attempt)
            pipeline_trace["patch_failure_evidence"].append({
                "failure_category": evidence.get("category"),
                "target": evidence.get("file"),
                "symbol": attempt["symbol"],
                "concise_error": evidence.get("deterministic_message", "")[:500],
                "fingerprint": evidence.get("previous_patch_fingerprint"),
                "repair_count": repair_count,
                "final_attempt_state": attempt["terminal_reason"],
                **evidence,
            })
            pipeline_trace["terminal_reason"] = evidence.get("category")

        # Transaction locale de tâche : même lorsqu'il est utilisé directement (sans
        # ImprovementOrchestrator), un DeveloperAgent qui finit en échec ne laisse
        # jamais derrière lui un candidat rouge.
        task_baseline: dict[str, tuple[bool, str]] = {}
        for file_path in files:
            path = Path(file_path)
            existed = path.is_file()
            content = path.read_text(encoding="utf-8", errors="replace") if existed else ""
            task_baseline[str(path)] = (existed, content)

        def restore_task_baseline() -> bool:
            nonlocal rollback_performed
            restored = True
            for raw_path, (existed, content) in task_baseline.items():
                try:
                    path = Path(raw_path)
                    if existed:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content, encoding="utf-8", newline="")
                    else:
                        path.unlink(missing_ok=True)
                except Exception:
                    restored = False
            rollback_performed = rollback_performed or (bool(written_files) and restored)
            return restored

        def make_result(success, *, iteration, tests_run, tests_passed, failure_reason):
            if not success and written_files:
                restored = restore_task_baseline()
                if not restored:
                    failure_reason = f"{failure_reason or 'task_failed'}; task_rollback_incomplete"
            explorations = [item for item in [exploration, *diagnostic_explorations] if item is not None]
            diagnostic_attempts = sum(int(item.model_calls or 0) for item in diagnostic_explorations)
            total_model_attempts = int(model_attempts or 0) + int(generation_calls) + diagnostic_attempts
            if review_result is not None:
                total_model_attempts += int(review_result.model_calls or 0)
            confidence = calibrate_developer_confidence(
                tests_requested=bool(tests_run),
                tests_passed=bool(tests_passed),
                tool_calls=sum(len(item.observations) for item in explorations),
                iterations=int(iteration or 0),
                reviewer_decision=review_result.decision if review_result else None,
                reviewer_confidence=review_result.confidence if review_result else 0.0,
                replan_requested=replan_requested,
            )
            return DeveloperResult(
                success=success,
                task=task_name,
                plan=plan,
                files_changed=list(dict.fromkeys(changed_files)),
                iterations=iteration,
                tests_run=list(tests_run),
                tests_passed=tests_passed,
                failure_reason=failure_reason,
                diff_summary="; ".join(summaries) or "Aucune modification réelle.",
                tests_requested=list(task.tests),
                files_targeted=list(files),
                files_written=list(dict.fromkeys(written_files)),
                attempt_history=list(history),
                dry_run=task.dry_run,
                model_used=model_used,
                model_attempts=(usage_root.used_calls - initial_provider_attempts if usage_root is not None
                                else total_model_attempts or None),
                logical_model_requests=(usage_root.logical_requests - initial_logical_requests
                                        if usage_root is not None else None),
                model_duration_ms=model_duration_ms,
                files_created=list(dict.fromkeys(created_files)),
                tests_inferred=[test for test in tests_run if test not in task.tests],
                replan_requested=replan_requested,
                recommended_files=list(recommended_files),
                recommended_tests=list(recommended_tests),
                exploration_summary="\n\n".join(item.brief for item in explorations if item.brief),
                tool_trace=[obs.to_dict() for item in explorations for obs in item.observations],
                tool_calls=sum(len(item.observations) for item in explorations),
                rollback_performed=rollback_performed,
                exploration_model_turns=sum(int(item.model_turns or 0) for item in explorations),
                review_decision=review_result.decision if review_result else None,
                reviewer_confidence=review_result.confidence if review_result else 0.0,
                reviewer_summary=review_result.summary if review_result else "",
                reviewer_concerns=list(review_result.concerns) if review_result else [],
                confidence_score=confidence.score,
                confidence_band=confidence.band,
                regression_probe_status=(regression_probe.reason if regression_probe else "not_run"),
                dependency_findings=list(dependency_findings),
                status="success" if success else ("uncertain" if pipeline_trace.get("infrastructure_failure") else ("needs_replan" if replan_requested else "failed")),
                summary="; ".join(summaries) or (failure_reason or "Aucune modification reelle."),
                files_examined=list(dict.fromkeys(files)),
                files_modified=list(dict.fromkeys(changed_files)),
                tests_added=[path for path in created_files if Path(path).name.casefold().startswith("test_")],
                commands_run=[f"pytest {path}" for path in tests_run],
                errors=[failure_reason] if failure_reason else [],
                needs_replan=replan_requested,
                replan_reason=(failure_reason if replan_requested else None),
                confidence=confidence.score,
                pipeline_trace=dict(pipeline_trace),
                repair_attempted=repair_attempted,
                repair_count=repair_count,
                failure_before_repair=failure_before_repair.to_dict() if failure_before_repair else None,
                result_after_repair=result_after_repair,
            )


        if not files:
            return make_result(False, iteration=0, tests_run=[], tests_passed=False, failure_reason="Aucun fichier autorisé pour cette tâche.")

        # Supervised execution validates the exact candidate workspace before
        # any exploration, planning or patch-generation call by the Developer.
        if task.execution_limits is not None:
            baseline_tests = list(task.tests) or self._infer_tests(files)
            safe_tests = self._safe_test_paths(baseline_tests)
            if (not safe_tests or len(safe_tests) != len(baseline_tests)
                    or any(not (self.repo_root / test.split("::", 1)[0]).is_file() for test in safe_tests)):
                return make_result(False, iteration=0, tests_run=[], tests_passed=False,
                                   failure_reason="TEST_INFRA_FAILURE: selected public tests missing or unsafe")
            worker_tests, excluded_protected_tests = select_worker_safe_tests(self.repo_root, safe_tests)
            if not worker_tests:
                return make_result(False, iteration=0, tests_run=[], tests_passed=False,
                                   failure_reason="TEST_INFRA_FAILURE: no worker-visible prevalidation tests")
            self._baseline_preflight = True
            try:
                baseline_result = self._run_tests(worker_tests, self.repo_root)
            except Exception as exc:
                baseline_result = {"passed": False, "output_tail": "TEST_INFRA_FAILURE: " + str(exc)}
            finally:
                self._baseline_preflight = False
            baseline_result["excluded_protected_tests"] = excluded_protected_tests
            pipeline_trace["baseline_tests"] = baseline_result
            if not baseline_result.get("passed"):
                summary = parse_test_failure(baseline_result.get("output_tail") or "")
                if summary.failure_type in {"TEST_INFRA_FAILURE", "IMPORT_FAILURE", "SYNTAX_FAILURE"}:
                    pipeline_trace["infrastructure_failure"] = True
                    return make_result(False, iteration=0, tests_run=worker_tests, tests_passed=False,
                                       failure_reason="TEST_INFRA_FAILURE: " + summary.traceback_excerpt)

        from self_improvement.objective_preflight import literal_keyword_objective_satisfied
        if not task.grounded_symbol and len(files) == 1 and Path(files[0]).is_file() and literal_keyword_objective_satisfied(task_name, self._read_repo_file(files[0])):
            replan_requested = True
            pipeline_trace["terminal_reason"] = "ALREADY_SATISFIED"
            return make_result(False, iteration=0, tests_run=[], tests_passed=False,
                               failure_reason="ALREADY_SATISFIED: explicit literal keyword contract is present")

        evidence_pack = None
        if len(files) == 1 and Path(files[0]).is_file() and Path(files[0]).suffix.casefold() == ".py":
            try:
                evidence_pack = build_root_cause_evidence(
                    self.repo_intelligence, target_path=files[0], target_symbol=task.grounded_symbol,
                    expected_behavior=task.expected_behavior or task_name, observed_behavior=task.observed_behavior,
                    tests=task.tests, prior_failures=task.prior_failures,
                )
                pipeline_trace["root_cause_evidence"] = evidence_pack.to_dict()
            except (OSError, ValueError):
                evidence_pack = None
        if evidence_pack and evidence_pack.target_appears_satisfied:
            replan_requested = True
            recommended_files = list(dict.fromkeys(item.path for item in evidence_pack.symbols if item.relation != "suspected_target"))[:4]
            recommended_tests = list(evidence_pack.relevant_tests)
            pipeline_trace["terminal_reason"] = "TARGET_NOT_ROOT_CAUSE"
            pipeline_trace["strategy_escalation"] = {"level": "REPLAN_CAUSAL_PATH", "reason": evidence_pack.escalation_reason, "invalidated_target": evidence_pack.suspected_target}
            return make_result(False, iteration=0, tests_run=[], tests_passed=False, failure_reason="TARGET_NOT_ROOT_CAUSE: target already contains explicit expected behavior; replan caller/adapter/state boundary")

        # Un lot composé uniquement de nouveaux fichiers possède déjà un périmètre
        # déterministe : il n'existe aucun symbole cible à explorer ou à localiser.
        # Le générateur recevra malgré tout le contexte public borné du repository.
        all_targets_new = all(not Path(file_path).is_file() for file_path in files)
        if task.candidate_first or task.deterministic_handoff:
            exploration = DeveloperExplorationResult(
                "fallback",
                reason=("post_replan_targets_reused" if task.candidate_first else "planner_contract_reused"),
                model_calls=0,
            )
        elif all_targets_new:
            exploration = DeveloperExplorationResult("fallback", reason="all_targets_are_new")
        else:
            # Phase agentique read-only : le modèle peut explorer le repository avant
            # d'écrire. Il peut demander une replanification mais jamais élargir lui-même
            # le périmètre d'écriture de cette tâche.
            try:
                exploration = self._explore_before_edit(
                    task_name, files, list(task.tests), list(task.docs_domains),
                    max_steps=1 if task.candidate_first else None,
                )
            except Exception as exc:
                exploration = DeveloperExplorationResult("fallback", reason=f"exploration_crash: {exc}")

        if exploration and exploration.decision == "replan":
            replan_requested = True
            recommended_files = list(exploration.recommended_files)
            recommended_tests = list(exploration.recommended_tests)
            plan = exploration.brief or "Les cibles du Planner ne correspondent pas au repository observé."
            model_used = exploration.model_used
            model_attempts = max(1, exploration.model_calls)
            return make_result(
                False,
                iteration=0,
                tests_run=[],
                tests_passed=False,
                failure_reason=(
                    f"replan_requested: {exploration.reason}; "
                    f"recommended_files={recommended_files}; recommended_tests={recommended_tests}"
                ),
            )

        if exploration and exploration.decision == "proceed":
            plan = exploration.brief or "Plan fondé sur l'exploration du repository."
            model_used = exploration.model_used
            model_attempts = max(1, exploration.model_calls)
            model_duration_ms = None
        elif all_targets_new:
            plan = "Créer et valider atomiquement les nouveaux fichiers approuvés."
            model_used = None
            model_attempts = None
            model_duration_ms = None
        elif task.candidate_first or task.deterministic_handoff:
            plan = "Plan minimal dérivé du Planner : patch ciblé, syntaxe, tests publics, puis handoff."
            model_used = None
            model_attempts = int(exploration.model_calls or 0) if exploration else None
            model_duration_ms = None
        else:
            plan, model_used, plan_attempts, model_duration_ms = self._llm_plan(task_name, files)
            exploration_attempts = exploration.model_calls if exploration else 0
            if plan_attempts is None and exploration_attempts == 0:
                model_attempts = None
            else:
                model_attempts = int(plan_attempts or 0) + int(exploration_attempts)

        max_attempts = min(max(1, task.max_iterations), 3)
        base_instruction = f"{task_name} | {task.constraints and 'Contraintes: ' + '; '.join(task.constraints) or ''}"
        if evidence_pack is not None:
            base_instruction += "\n\n" + evidence_pack.render()
        if exploration and exploration.decision == "proceed" and exploration.brief:
            base_instruction += "\n\nBRIEF APRÈS EXPLORATION DU REPOSITORY :\n" + exploration.brief
        instruction = base_instruction
        strategy = "direct_replace"
        last_failure = None
        tests_run = list(task.tests)
        if not tests_run and exploration and exploration.recommended_tests:
            tests_run = [
                test for test in exploration.recommended_tests
                if (self.repo_root / test).is_file()
            ][:8]
        if not tests_run:
            tests_run = self._infer_tests(files)
        pipeline_trace["public_tests_selected"] = bool(tests_run)
        tests_passed = False

        for iteration in range(1, max_attempts + 1):
            attempt_fingerprint = None
            candidate_content = None
            patch_trace: dict = {}
            relevance_status = "not_run"
            tests_status = "not_run"
            try:
                # ==================================================
                # PHASE PREFLIGHT : générer et valider TOUS les
                # candidats SANS aucune écriture. Si un candidat
                # échoue, zéro écriture sur tous les fichiers.
                # ==================================================
                candidates: list[tuple[str, str, str]] = []  # (path, original, new_content)
                generator = self.patch_generator or self._default_patch_generator

                for file_path in files:
                    resolved_file = Path(file_path)
                    if not self._is_repo_allowed(str(resolved_file)):
                        raise ValueError(f"safety_rejection: chemin non autorisé : {file_path}")
                    file_existed = resolved_file.is_file()
                    original = self._read_repo_file(resolved_file) if file_existed else ""
                    if file_existed and self._contains_probable_secret(original):
                        raise ValueError(
                            f"safety_rejection: secret probable détecté dans {resolved_file.name}; "
                            "édition LLM distante refusée."
                        )
                    file_instruction = instruction
                    behavior_contract = required_behavior_contract(task_name)
                    if behavior_contract:
                        file_instruction += "\n\n" + behavior_contract
                    if candidates:
                        file_instruction += (
                            "\n\nINTERFACES VALIDÉES DES CANDIDATS PRÉCÉDENTS "
                            "(chemins/modules/symboles exacts; aucun contenu complet) :\n"
                            + self._candidate_interface_context(candidates)
                        )
                    if exploration and exploration.decision == "proceed":
                        try:
                            rel_for_instruction = resolved_file.resolve().relative_to(self.repo_root).as_posix()
                        except ValueError:
                            rel_for_instruction = ""
                        specific = exploration.file_instructions.get(rel_for_instruction)
                        if specific:
                            file_instruction += "\n\nINSTRUCTION SPÉCIFIQUE À CE FICHIER :\n" + specific
                    if file_existed:
                        try:
                            if self.patch_generator is None:
                                generation_calls += 1
                            patched = generator(resolved_file, file_instruction)
                            patch_trace = dict(getattr(patched, "trace", {}) or {})
                            if patch_trace:
                                pipeline_trace["patch_generation"] = patch_trace
                                pipeline_trace["pretest_patch_repair"] = bool(
                                    patch_trace.get("pretest_repair_used")
                                )
                            old_content, new_content, _ = self._unpack_patch(patched)
                        except Exception as exc:
                            if getattr(exc, "pretest_repair_exhausted", False):
                                raise
                            error_text = str(exc)
                            if not (self._is_ambiguous_patch_error(error_text) or self._is_structured_patch_error(error_text)):
                                raise
                            retry_instruction = self._build_precision_retry_instruction(
                                str(resolved_file), error_text,
                                self._extract_ambiguous_context(original),
                            )
                            if self.patch_generator is None:
                                generation_calls += 1
                            patched = generator(resolved_file, retry_instruction)
                            old_content, new_content, _ = self._unpack_patch(patched)
                        pipeline_trace["developer_response_received"] = True
                        pipeline_trace["patch_proposal_parsed"] = True
                        pipeline_trace["patch_protocol_accepted"] = True
                    else:
                        creator = self.new_file_generator or self._default_new_file_generator
                        if self.new_file_generator is None:
                            generation_calls += 1
                        patched = creator(resolved_file, file_instruction)
                        old_content, new_content, _ = self._unpack_patch(patched)
                        if old_content != "":
                            raise ValueError("new_file_provenance_mismatch: un nouveau fichier doit avoir un original vide.")
                        if self.new_file_generator is None and resolved_file.suffix.casefold() == ".py":
                            syntax_ok, syntax_error = self._compile_candidate_contents([
                                (str(resolved_file), "", new_content),
                            ])
                            if not syntax_ok:
                                generation_calls += 1
                                retry_instruction = (
                                    file_instruction
                                    + "\n\nCORRECTION SYNTAXIQUE DU FICHIER DEMANDÉ :\n"
                                    + str(syntax_error)
                                    + "\nRetourne le fichier complet corrigé et syntaxiquement valide."
                                )
                                patched = creator(resolved_file, retry_instruction)
                                old_content, new_content, _ = self._unpack_patch(patched)
                                if old_content != "":
                                    raise ValueError(
                                        "new_file_provenance_mismatch: un nouveau fichier doit avoir un original vide."
                                    )

                    if old_content != original:
                        raise ValueError("patch_provenance_mismatch: le générateur a utilisé un contenu source différent du contenu lu.")
                    if self._contains_probable_secret(new_content):
                        raise ValueError(
                            f"safety_rejection: secret probable détecté dans le candidat {resolved_file.name}; "
                            "écriture refusée."
                        )
                    if resolved_file.suffix.casefold() == ".py":
                        escalations = introduced_restricted_capabilities(original, new_content)
                        if escalations:
                            rendered = ", ".join(
                                f"{name}(+{count})" for name, count in sorted(escalations.items())
                            )
                            raise ValueError(
                                "safety_rejection: le candidat introduit une primitive "
                                f"d'exécution restreinte : {rendered}"
                            )
                        candidate_syntax_ok, candidate_syntax_error = self._compile_candidate_contents([
                            (str(resolved_file), original, new_content),
                        ])
                        if not candidate_syntax_ok:
                            raise ValueError(f"syntax_error: {candidate_syntax_error}")
                    fingerprint = patch_fingerprint(original, new_content)
                    attempt_fingerprint = fingerprint
                    if fingerprint in failed_fingerprints:
                        pipeline_trace["duplicate_patch"] = True
                        pipeline_trace["no_progress"] = True
                        raise ValueError("duplicate_failed_attempt: le retry produit exactement le même patch.")
                    candidate_content = new_content
                    content_fingerprint = patch_fingerprint("", candidate_content)
                    if content_fingerprint in failed_content_fingerprints:
                        pipeline_trace["duplicate_patch"] = True
                        pipeline_trace["no_progress"] = True
                        raise ValueError("duplicate_failed_attempt: le retry reproduit le même contenu candidat.")
                    if old_content == new_content:
                        # For a single-file task, no change is an immediate failure.
                        # For multi-file tasks, a file with no change is skipped
                        # (the change may live in another file in the batch).
                        if len(files) == 1:
                            raise ValueError("no_change: le candidat ne modifie pas le fichier.")
                        continue
                    candidates.append((file_path, original, new_content))

                # Guard: if ALL files produced no change, fail with no_change.
                if not candidates:
                    raise ValueError("no_change: aucun fichier modifié dans le lot.")

                if repair_attempted and previous_candidates:
                    if candidates == previous_candidates or [item[2] for item in candidates] == [item[2] for item in previous_candidates]:
                        pipeline_trace["repair_delta"] = "PATCH_REPAIR_NO_PROGRESS"
                        pipeline_trace["no_progress"] = True
                        raise ValueError("PATCH_REPAIR_NO_PROGRESS: repaired patch is identical to the failed patch.")
                    pipeline_trace["repair_delta"] = "changed"

                tests_discoverable, discovery_error = self._validate_generated_test_discovery(
                    candidates, list(task.tests)
                )
                if not tests_discoverable:
                    raise ValueError(discovery_error or "generated_test_not_discoverable")

                imports_ok, import_error = self._validate_intra_batch_imports(candidates)
                if not imports_ok:
                    raise ValueError(import_error or "INTRA_BATCH_IMPORT_VALIDATION")

                # Validate syntax before relevance: AST name extraction is empty
                # for invalid Python and would otherwise mask the actual cause.
                syntax_ok, syntax_error = self._compile_candidate_contents(candidates)
                if not syntax_ok:
                    raise ValueError(f"syntax_error: {syntax_error}")
                pipeline_trace["syntax_passed"] = True
                from self_improvement.symbol_contract import validate_method_contracts
                for candidate_path, before, after in candidates:
                    if Path(candidate_path).suffix.casefold() == ".py":
                        validate_method_contracts(before, after)

                # Relevance check: one call across ALL candidates combined.
                # This prevents false rejections on multi-file tasks where
                # required identifiers are spread across different files.
                file_pairs = [(orig, new) for _, orig, new in candidates]
                if len(file_pairs) > 1:
                    relevance = check_patch_relevance_multi(relevance_task, file_pairs)
                else:
                    relevance = check_patch_relevance(relevance_task, file_pairs[0][0], file_pairs[0][1])
                relevance_status = "passed" if relevance.passed else ("abstained" if relevance.abstained else "failed")
                if relevance_repair_attempted:
                    pipeline_trace["requirement_coverage_after"] = relevance.requirement_coverage
                if not relevance.passed:
                    coverage_key = (
                        "requirement_coverage_after"
                        if relevance_repair_attempted
                        else "requirement_coverage_before"
                    )
                    pipeline_trace[coverage_key] = relevance.requirement_coverage
                    missing_identifiers = sorted(
                        set(relevance.required_identifiers) - set(relevance.matched_identifiers)
                    )
                    raise ValueError(
                        f"{relevance.reason}: "
                        f"missing={missing_identifiers}; "
                        f"matched={relevance.matched_identifiers}; "
                        f"added={relevance.added_symbols}"
                    )
                # Quality gate Ruff sur le CONTENU CANDIDAT (avant écriture,
                # non bloquant si Ruff est absent).
                ruff_ok, ruff_error = self._run_ruff_check_candidates(candidates)
                if not ruff_ok:
                    raise ValueError(ruff_error or "quality_gate_failure: ruff a signalé des erreurs.")

                dependency_check = self.dependency_guard.inspect_candidates(candidates)
                dependency_findings = list(dependency_check.missing_from_requirements)
                if dependency_findings:
                    requirements_path = dependency_check.requirements_file or "requirements.txt"
                    approved_rel = set()
                    for item in files:
                        try:
                            approved_rel.add(Path(item).resolve(strict=False).relative_to(self.repo_root).as_posix())
                        except ValueError:
                            pass
                    if requirements_path not in approved_rel:
                        replan_requested = True
                        recommended_files = list(dict.fromkeys([
                            *[Path(item).resolve(strict=False).relative_to(self.repo_root).as_posix() for item in files],
                            requirements_path,
                        ]))
                        return make_result(
                            False, iteration=iteration, tests_run=tests_run, tests_passed=False,
                            failure_reason=(
                                "replan_requested_missing_dependency_declaration: "
                                + ", ".join(dependency_findings[:6])
                            ),
                        )
                    raise ValueError(
                        "dependency_declaration_missing: imports tiers non déclarés: "
                        + ", ".join(dependency_findings[:6])
                    )

                from self_improvement.execution_limits import check_candidate
                cumulative = {
                    path: (original, Path(path).read_text(encoding="utf-8", errors="replace")
                           if Path(path).is_file() else "")
                    for path, (_existed, original) in task_baseline.items()
                }
                for path, original, new_content in candidates:
                    baseline = task_baseline.get(str(path), (False, original))[1]
                    cumulative[str(path)] = (baseline, new_content)
                check_candidate(cumulative, task.max_source_files, task.max_diff_lines)
                if task.execution_limits is not None:
                    from self_improvement.execution_limits import ExecutionLimits
                    ExecutionLimits.from_dict(task.execution_limits).phase_timeout(120, minimum=120, future_seconds=420.0)

                if task.require_regression_proof and not task.dry_run:
                    if self._execution_limits is not None:
                        self._execution_limits.phase_timeout(90, minimum=90, future_seconds=540.0)
                    regression_probe = self.regression_test_guard.verify_new_tests_fail_on_baseline(candidates)
                    if regression_probe.checked and not regression_probe.baseline_failed:
                        raise ValueError(
                            "regression_test_not_demonstrated: le nouveau test passe déjà sur la baseline"
                        )

                if task.dry_run:
                    for file_path, original, new_content in candidates:
                        if file_path not in changed_files:
                            changed_files.append(file_path)
                        summaries.append(self._diff_summary(file_path, original, new_content))
                    history.append(AttemptRecord(
                        iteration, strategy, files, attempt_fingerprint,
                        "accepted", relevance_status=relevance_status,
                    ).as_dict())
                    return make_result(True, iteration=iteration, tests_run=[], tests_passed=False, failure_reason=None)

                # ==================================================
                # PHASE ÉCRITURE ATOMIQUE : on écrit les fichiers un
                # par un. Si une écriture échoue, on restaure ceux
                # déjà écrits depuis les originaux connus.
                # ==================================================
                written_pairs: list[tuple[str, str, bool]] = []  # (path, original_content, existed_before)
                try:
                    for file_path, original, new_content in candidates:
                        if file_path not in changed_files:
                            changed_files.append(file_path)
                        summaries.append(self._diff_summary(file_path, original, new_content))
                        existed_before = Path(file_path).is_file()
                        if self._apply_patch(str(Path(file_path)), original, new_content):
                            written_pairs.append((file_path, original, existed_before))
                            if file_path not in written_files:
                                written_files.append(file_path)
                            if not existed_before and file_path not in created_files:
                                created_files.append(file_path)
                    pipeline_trace["patch_applied"] = bool(written_pairs)
                except Exception as write_exc:
                    # ROLLBACK : restaurer tous les fichiers déjà écrits.
                    for rolled_path, rolled_original, existed_before in reversed(written_pairs):
                        try:
                            rolled = Path(rolled_path)
                            if existed_before:
                                rolled.write_text(rolled_original, encoding="utf-8", newline="")
                            else:
                                rolled.unlink(missing_ok=True)
                        except Exception:  # pragma: no cover - best-effort rollback
                            pass
                    raise ValueError(f"write_error: échec d'écriture atomique, rollback effectué. Détail : {write_exc}") from write_exc

                if self._uses_default_test_runner and tests_run:
                    requested_tests = list(dict.fromkeys(tests_run))
                    safe_tests = self._safe_test_paths(requested_tests)
                    if safe_tests != requested_tests:
                        rejected = [item for item in requested_tests if item not in safe_tests]
                        raise ValueError(
                            f"safety_rejection: chemins de tests non autorisés/introuvables : {rejected[:4]}"
                        )
                    tests_run = safe_tests
                test_result = self._run_tests(tests_run, self.repo_root)
                pipeline_trace["public_tests_executed"] = bool(tests_run)
                tests_run = list(test_result.get("tests_run") or tests_run)
                tests_passed = bool(tests_run) and bool(test_result.get("passed"))
                pipeline_trace["public_tests_passed"] = tests_passed
                if repair_attempted:
                    pipeline_trace["repaired_tests"] = dict(test_result)
                if tests_run:
                    tests_status = "passed" if tests_passed else "failed"
                elif test_result.get("passed") or self._uses_default_test_runner:
                    tests_status = "not_run"
                else:
                    tests_status = "failed"
                if not test_result.get("passed") and (tests_run or not self._uses_default_test_runner):
                    summary = parse_test_failure(
                        test_result.get("output_tail") or "",
                        changed_symbols=changed_python_symbols(candidates),
                    )
                    if summary.failure_type == "TEST_INFRA_FAILURE":
                        pipeline_trace["test_failure_summary"] = summary.to_dict()
                        pipeline_trace["infrastructure_failure"] = True
                        return make_result(
                            False, iteration=iteration, tests_run=tests_run,
                            tests_passed=False, failure_reason="TEST_INFRA_FAILURE: " + summary.traceback_excerpt,
                        )
                if not tests_run and not test_result.get("passed") and not self._uses_default_test_runner:
                    # Token efficiency : tronquer l'output long à 2000 chars pour le retry.
                    output = test_result.get("output_tail") or ""
                    raise ValueError(output[:2000] or "test_failure: runner injecté en échec.")
                if tests_run and not tests_passed:
                    output = test_result.get("output_tail") or ""
                    if not task.candidate_first:
                        raise ValueError(output[:2000] or "test_failure: tests ciblés en échec.")
                    summary = parse_test_failure(output, changed_symbols=changed_python_symbols(candidates))
                    pipeline_trace["test_failure_summary"] = summary.to_dict()
                    if repair_attempted and failure_before_repair is not None:
                        progress = compare_failures(failure_before_repair, summary)
                        result_after_repair = {"passed": False, "progress": progress.value, "failure": summary.to_dict()}
                        if progress is RepairProgress.REGRESSION:
                            raise ValueError("REGRESSION: semantic repair introduced new public-test failures.")
                        if progress is RepairProgress.NO_PROGRESS:
                            raise ValueError("PATCH_REPAIR_NO_PROGRESS: same public-test failures after repair.")
                        raise ValueError("PARTIAL_PROGRESS: semantic repair changed failures but candidate is not ready.")
                    failure_before_repair = summary
                    previous_candidates = list(candidates)
                    pipeline_trace["patch_initial"] = [
                        {"file": path, "before": before, "after": after}
                        for path, before, after in candidates
                    ]
                    raise ValueError(output[:2000] or "test_failure: tests ciblés en échec.")

                review_result = self._review_after_tests(task_name, candidates, tests_run, tests_passed)
                pipeline_trace["reviewer_reached"] = review_result is not None
                if review_result is not None and review_result.decision == "request_changes" and review_result.confidence >= self.reviewer_threshold:
                    concerns = "; ".join(review_result.concerns[:4]) or review_result.summary or "reviewer concern"
                    raise ValueError(f"review_requested_changes: {concerns[:1800]}")

                failure = None
                decision = judge_patch(
                    changed=bool(changed_files),
                    syntax_ok=True,
                    relevance=relevance,
                    tests_requested=bool(tests_run),
                    tests_passed=tests_passed,
                    failure=failure,
                )
                pipeline_trace["developer_judge_reached"] = True
                if decision.accepted:
                    if repair_attempted:
                        result_after_repair = {"passed": True, "progress": RepairProgress.STRONG_PROGRESS.value}
                    pipeline_trace["candidate_ready"] = True
                    pipeline_trace["handoff_reason"] = "syntax_and_public_tests_passed"
                    history.append(AttemptRecord(
                        iteration, strategy, files, attempt_fingerprint, "accepted",
                        tests_status=tests_status, relevance_status=relevance_status,
                    ).as_dict())
                    return make_result(True, iteration=iteration, tests_run=tests_run, tests_passed=tests_passed, failure_reason=None)
                raise ValueError(decision.reason)
            except Exception as exc:
                last_failure = str(exc)
                if last_failure in {"candidate_source_file_limit_exceeded", "candidate_diff_limit_exceeded", "INSUFFICIENT_GLOBAL_TIME"}:
                    return make_result(False, iteration=iteration, tests_run=tests_run,
                                       tests_passed=False, failure_reason=last_failure)
                exhausted_pretest = bool(getattr(exc, "pretest_repair_exhausted", False))
                if exhausted_pretest:
                    patch_trace = dict(getattr(exc, "trace", {}) or {})
                    pipeline_trace["patch_generation"] = patch_trace
                    pipeline_trace["pretest_patch_repair"] = True
                    pipeline_trace["no_progress"] = bool(patch_trace.get("no_progress"))
                evidence = classify_patch_failure(
                    last_failure,
                    file=(str(files[0]) if files else None),
                    provider=getattr(exc, "provider", None),
                    model=getattr(exc, "model", None),
                    fingerprint=attempt_fingerprint,
                )
                record_patch_attempt(
                    attempt_id=iteration,
                    file_path=(str(files[0]) if files else None),
                    patch_trace=patch_trace,
                    error=exc,
                    evidence=evidence.to_dict(),
                )
                if not pipeline_trace.get("public_tests_executed"):
                    recorder = getattr(task.model_budget, "record_patch_failure", None)
                    if callable(recorder):
                        recorder(evidence.to_dict())
                analysis = analyze_failure(
                    last_failure,
                    relevance_reason=last_failure.split(":", 1)[0]
                    if last_failure.startswith((
                        "irrelevant_patch",
                        "relevance_missing_required_identifier",
                        "relevance_target_symbol_unchanged",
                        "no_change",
                    ))
                    else None,
                )
                # Only record failed fingerprints for hard, deterministic failures
                # (test failures, syntax errors, safety, provenance).  Relevance
                # failures are not recorded because the relevance gate can produce
                # false positives (e.g. multi-file tasks checked per-file previously),
                # and blocking the exact same patch on retry would prevent a valid
                # correction from succeeding.  no_change is also excluded: the same
                # no-change output must remain blockable via the loop guard above.
                _relevance_reasons = {
                    "irrelevant_patch",
                    "relevance_missing_required_identifier",
                    "relevance_target_symbol_unchanged",
                }
                _failure_is_relevance = analysis.type in _relevance_reasons
                if attempt_fingerprint and not _failure_is_relevance:
                    failed_fingerprints.add(attempt_fingerprint)
                if isinstance(candidate_content, str) and not _failure_is_relevance:
                    failed_content_fingerprints.add(patch_fingerprint("", candidate_content))
                history.append(AttemptRecord(
                    iteration, strategy, files, attempt_fingerprint, "failed",
                    analysis.type, analysis.reason, tests_status, relevance_status,
                ).as_dict())
                if task.candidate_first and analysis.type == "syntax_error" and failure_before_repair is None:
                    failure_before_repair = parse_test_failure(last_failure)
                    previous_candidates = list(candidates)
                    pipeline_trace["test_failure_summary"] = failure_before_repair.to_dict()
                    pipeline_trace["patch_initial"] = [
                        {"file": path, "before": before, "after": after}
                        for path, before, after in previous_candidates
                    ]
                if exhausted_pretest or iteration >= max_attempts or not analysis.retryable:
                    if exhausted_pretest and evidence.category in {"PATCH_SCHEMA_FAILURE", "PATCH_CONTRACT_FAILURE", "PATCH_PRETEST_FAILURE"}:
                        replan_requested = True
                        pipeline_trace["strategy_escalation"] = {
                            "level": "REPLAN_CAUSAL_PATH",
                            "reason": "bounded_local_patch_repair_exhausted",
                            "failure_category": evidence.category,
                            "invalidated_target": evidence_pack.suspected_target if evidence_pack else str(files[0]),
                        }
                    return make_result(
                        False, iteration=iteration,
                        tests_run=tests_run, tests_passed=False,
                        failure_reason=analysis.reason,
                    )

                if task.candidate_first and evidence.repairable:
                    pipeline_trace["pretest_patch_repair"] = True

                if task.candidate_first and analysis.type in {
                    "relevance_missing_required_identifier", "irrelevant_patch",
                    "relevance_target_symbol_unchanged",
                }:
                    if relevance_repair_attempted:
                        pipeline_trace["no_progress"] = True
                        pipeline_trace["repair_delta"] = "RELEVANCE_REPAIR_NO_PROGRESS"
                        return make_result(
                            False, iteration=iteration, tests_run=tests_run,
                            tests_passed=False,
                            failure_reason=f"RELEVANCE_REPAIR_NO_PROGRESS: {analysis.reason}",
                        )
                    relevance_repair_attempted = True
                    pipeline_trace["relevance_repair"] = True
                    missing = []
                    coverage = getattr(locals().get("relevance"), "requirement_coverage", [])
                    for item in coverage:
                        if item.get("status") != "COVERED":
                            missing.append(str(item.get("expected_behavior")))
                    changed = changed_python_symbols(candidates)
                    instruction = (
                        f"OBJECTIVE: {task_name}\n"
                        f"RELEVANCE REPAIR (one attempt): implement missing behaviors: {missing}.\n"
                        f"TARGET SYMBOLS ALREADY CHANGED: {changed}.\n"
                        f"ALLOWED FILES: {[Path(path).name for path in files]}.\n"
                        "Keep the same objective and scope. Change the responsible source symbol; "
                        "do not edit tests and do not merely add names or comments."
                    )
                    continue

                if task.candidate_first and analysis.type == "patch_target_failure":
                    if target_repair_attempted:
                        pipeline_trace["no_progress"] = True
                        return make_result(
                            False, iteration=iteration, tests_run=tests_run,
                            tests_passed=False,
                            failure_reason=f"PATCH_TARGET_REPAIR_NO_PROGRESS: {analysis.reason}",
                        )
                    target_repair_attempted = True
                    pipeline_trace["target_repair"] = True
                    instruction = (
                        f"OBJECTIVE: {task_name}\n"
                        "TARGET REPAIR (one attempt): use target_id T1.\n"
                        f"ALLOWED FILES: {[Path(path).name for path in files]}.\n"
                        "Choose an existing target symbol from the supplied canonical target context. "
                        "Do not change scope, objective, or tests."
                    )
                    continue

                if failure_before_repair is not None:
                    if repair_attempted:
                        return make_result(False, iteration=iteration, tests_run=tests_run,
                                           tests_passed=False, failure_reason=analysis.reason)
                    repair_attempted = True
                    repair_count = 1
                    pipeline_trace["semantic_repair"] = True
                    instruction = build_repair_instruction(
                        objective=task_name,
                        previous_patch=[{"file": path, "before": before, "after": after}
                                        for path, before, after in previous_candidates],
                        summary=failure_before_repair,
                        target_symbols=failure_before_repair.candidate_changed_symbols,
                        allowed_files=[Path(path).resolve().relative_to(self.repo_root).as_posix() for path in files],
                        constraints=task.constraints,
                    )
                    continue

                # Une tentative rouge ne devient jamais la source du retry. On
                # restaure d'abord la baseline locale afin qu'un fichier créé lors
                # de l'itération précédente reste traité comme une création, et non
                # comme un patch d'un candidat déjà invalide.
                if written_files and all_targets_new:
                    if not restore_task_baseline():
                        return make_result(
                            False, iteration=iteration,
                            tests_run=tests_run, tests_passed=False,
                            failure_reason=f"{analysis.reason}; task_rollback_incomplete",
                        )
                    changed_files.clear()
                    written_files.clear()
                    created_files.clear()
                    summaries.clear()

                # Après un échec réel, l'agent peut relire le repository avant de
                # retenter. Il reste read-only et ne peut qu'enrichir le retry ou
                # demander au niveau supérieur de replanifier.
                diagnostic = None
                deterministic_failure_types = {
                    "syntax_error", "protocol_failure",
                    "patch_validation_failure", "patch_application_failure",
                    "relevance_missing_required_identifier", "irrelevant_patch",
                    "relevance_target_symbol_unchanged", "no_change",
                }
                deterministic_test_failure = (
                    analysis.type == "test_failure"
                    and any(marker in analysis.reason.casefold() for marker in (
                        "assert", "traceback", "failed", "failure", "expected", "actual",
                    ))
                    and not any(marker in analysis.reason.casefold() for marker in (
                        "missing dependency", "scope missing", "helper contract",
                    ))
                )
                if analysis.type in deterministic_failure_types or deterministic_test_failure:
                    pipeline_trace["deterministic_diagnostic_used"] = True
                elif not all_targets_new:
                    try:
                        diagnostic = self._diagnose_after_failure(
                            task_name, files, tests_run, analysis.reason or last_failure,
                            list(task.docs_domains),
                        )
                    except Exception as diagnostic_exc:
                        diagnostic = DeveloperExplorationResult(
                            "fallback", reason=f"failure_diagnostic_crash: {diagnostic_exc}"
                        )
                if diagnostic is not None:
                    diagnostic_explorations.append(diagnostic)
                    if diagnostic.decision == "replan":
                        replan_requested = True
                        recommended_files = list(diagnostic.recommended_files)
                        recommended_tests = list(diagnostic.recommended_tests)
                        return make_result(
                            False, iteration=iteration, tests_run=tests_run, tests_passed=False,
                            failure_reason=(
                                f"replan_requested_after_failure: {diagnostic.reason}; "
                                f"recommended_files={recommended_files}; "
                                f"recommended_tests={recommended_tests}"
                            ),
                        )

                # Token efficiency : passer seulement le dernier échec au planner.
                strategy, instruction = plan_retry(task_name, analysis, history[-1:], strategy)
                if diagnostic is not None and diagnostic.decision == "proceed" and diagnostic.brief:
                    instruction += (
                        "\n\nDIAGNOSTIC READ-ONLY APRÈS ÉCHEC :\n" + diagnostic.brief
                    )

        return make_result(False, iteration=max_attempts, tests_run=tests_run, tests_passed=tests_passed, failure_reason=last_failure or "La boucle a atteint sa limite.")


def _parse_args():
    parser = argparse.ArgumentParser(description="Developer Agent V3 borné pour PROJET IA.")
    parser.add_argument("--task", required=True, help="Description naturelle de la tâche à réaliser.")
    parser.add_argument("--apply", action="store_true", help="Applique les modifications dans le dépôt.")
    parser.add_argument("--dry-run", action="store_true", help="Exécute la boucle sans écrire dans le dépôt.")
    parser.add_argument("--file", dest="files", action="append", default=[], help="Fichier cible (peut être répété).")
    parser.add_argument("--test", dest="tests", action="append", default=[], help="Test ciblé à exécuter (peut être répété).")
    parser.add_argument("--max-iterations", type=int, default=2, help="Nombre maximal de boucles de correction.")
    return parser.parse_args()


def main():
    print('Bienvenue dans le Developer Agent V3.')

    args = _parse_args()
    task = DeveloperTask(
        task=args.task,
        target_files=args.files,
        tests=args.tests,
        max_iterations=max(1, args.max_iterations),
        dry_run=bool(args.dry_run) and not args.apply,
    )
    agent = DeveloperAgent(repo_root=Path(__file__).resolve().parents[1])
    result = agent.run(task)
    print(json.dumps({
        "success": result.success,
        "task": result.task,
        "plan": result.plan,
        "files_changed": result.files_changed,
        "iterations": result.iterations,
        "tests_run": result.tests_run,
        "tests_passed": result.tests_passed,
        "failure_reason": result.failure_reason,
        "diff_summary": result.diff_summary,
        "dry_run": result.dry_run,
    }, ensure_ascii=False, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
