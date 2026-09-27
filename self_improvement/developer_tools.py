"""Boucle d'exploration read-only du Developer Agent.

Le modèle n'obtient jamais de shell ni d'écriture directe. Il peut uniquement
interroger une vue statique du repository, puis soit :
- produire un brief d'implémentation pour les fichiers déjà approuvés ;
- demander une replanification si les cibles du Planner sont manifestement fausses.

Cette séparation permet d'ajouter un comportement réellement agentique sans élargir
le périmètre transactionnel de la tâche.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Callable

from self_improvement.repo_intelligence import RepoIntelligence
from self_improvement.agent_path_policy import is_agent_editable_path, is_agent_readable_path
from self_improvement.developer_docs import OfficialDocsExplorer, validate_requested_doc_domains
from self_improvement.engineering_memory import EngineeringMemory
from self_improvement.developer_strategy import explorer_initial_prompt, explorer_observation_prompt


ChatFunction = Callable[..., dict[str, Any]]
_SAFE_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SAFE_QUALIFIED_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}(?:\.[A-Za-z_][A-Za-z0-9_]{0,127}){0,7}$")


@dataclass
class ToolObservation:
    step: int
    action: str
    arguments: dict[str, Any]
    result: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeveloperExplorationResult:
    decision: str  # proceed | replan | fallback
    brief: str = ""
    file_instructions: dict[str, str] = field(default_factory=dict)
    recommended_files: list[str] = field(default_factory=list)
    recommended_tests: list[str] = field(default_factory=list)
    observations: list[ToolObservation] = field(default_factory=list)
    model_calls: int = 0  # tentatives provider cumulées (budget)
    model_turns: int = 0  # tours logiques de la boucle d'exploration
    model_used: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "brief": self.brief,
            "file_instructions": dict(self.file_instructions),
            "recommended_files": list(self.recommended_files),
            "recommended_tests": list(self.recommended_tests),
            "observations": [item.to_dict() for item in self.observations],
            "model_calls": self.model_calls,
            "model_turns": self.model_turns,
            "model_used": self.model_used,
            "reason": self.reason,
        }


class ReadOnlyDeveloperExplorer:
    """Observe -> outil -> observe -> ... -> proceed/replan, avec budget dur."""

    ACTIONS = frozenset({
        "search_code",
        "find_symbol",
        "read_symbol",
        "read_file",
        "inspect_file",
        "find_tests",
        "references_to",
        "dependencies",
        "search_docs",
        "read_docs",
        "memory_hints",
        "proceed",
        "replan",
    })

    def __init__(
        self,
        repo_root: str | Path,
        *,
        chat_function: ChatFunction,
        repo_intelligence: RepoIntelligence | None = None,
        max_steps: int = 10,
        max_observation_chars: int = 28_000,
        docs_domains: list[str] | None = None,
        docs_explorer: OfficialDocsExplorer | None = None,
        engineering_memory: EngineeringMemory | None = None,
        model_budget: Any = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.chat_function = chat_function
        self.repo_intelligence = repo_intelligence or RepoIntelligence(self.repo_root)
        self.max_steps = max(1, min(int(max_steps), 16))
        self.max_observation_chars = max(2_000, min(int(max_observation_chars), 40_000))
        self.docs_domains = validate_requested_doc_domains(docs_domains or [])
        self.docs_explorer = docs_explorer or (OfficialDocsExplorer(self.docs_domains) if self.docs_domains else None)
        self.engineering_memory = engineering_memory or EngineeringMemory(self.repo_root)
        self.model_budget = model_budget

    def explore(
        self,
        task: str,
        *,
        approved_files: list[str],
        requested_tests: list[str] | None = None,
    ) -> DeveloperExplorationResult:
        approved_rel = [self._relative(path, require_editable=True) for path in approved_files]
        approved_rel = [path for path in approved_rel if path]
        requested_tests = list(requested_tests or [])
        initial = self._initial_context(task, approved_rel, requested_tests)
        messages: list[dict[str, str]] = [{"role": "user", "content": initial}]
        observations: list[ToolObservation] = []
        seen_actions: set[str] = set()
        total_chars = 0
        model_calls = 0
        model_turns = 0
        last_model: str | None = None

        phase = "developer_diagnose" if "ÉCHEC OBSERVÉ APRÈS MODIFICATION/TEST" in task else "developer_explore"
        for step in range(1, self.max_steps + 1):
            try:
                budget_kwargs = {"model_budget": self.model_budget} if self.model_budget is not None else {}
                response = self.chat_function(
                    messages=messages,
                    task_type=phase,
                    format=self._schema(),
                    options={"temperature": 0},
                    think=False,
                    **budget_kwargs,
                )
                model_turns += 1
                if isinstance(response, dict):
                    meta = response.get("_meta", {}) or {}
                    last_model = meta.get("model") or last_model
                    try:
                        provider_attempts = max(1, int(meta.get("attempts") or 1))
                    except (TypeError, ValueError):
                        provider_attempts = 1
                else:
                    provider_attempts = 1
                model_calls += provider_attempts
                action = self._parse_action(response)
            except Exception as exc:
                return DeveloperExplorationResult(
                    "fallback",
                    observations=observations,
                    model_calls=model_calls,
                    model_turns=model_turns,
                    model_used=last_model,
                    reason=f"exploration_model_error: {exc}",
                )

            name = action.get("action", "").strip().casefold()
            if name not in self.ACTIONS:
                return DeveloperExplorationResult(
                    "fallback",
                    observations=observations,
                    model_calls=model_calls,
                    model_turns=model_turns,
                    model_used=last_model,
                    reason=f"invalid_exploration_action: {name or 'empty'}",
                )

            if name in {"proceed", "replan"}:
                return self._finish(
                    name,
                    action,
                    approved_rel,
                    observations,
                    model_calls,
                    model_turns,
                    last_model,
                )

            try:
                args = self._bounded_arguments(name, action)
            except Exception as exc:
                # Un argument interdit (holdout, chemin hors repo, symbole invalide...)
                # coupe l'exploration au lieu de laisser le modèle sonder le périmètre.
                return DeveloperExplorationResult(
                    "fallback",
                    observations=observations,
                    model_calls=model_calls,
                    model_turns=model_turns,
                    model_used=last_model,
                    reason=f"invalid_tool_arguments: {exc}",
                )
            fingerprint = json.dumps([name, args], ensure_ascii=False, sort_keys=True)
            if fingerprint in seen_actions:
                return DeveloperExplorationResult(
                    "fallback",
                    observations=observations,
                    model_calls=model_calls,
                    model_turns=model_turns,
                    model_used=last_model,
                    reason="repeated_tool_action",
                )
            seen_actions.add(fingerprint)

            try:
                result = self._execute(name, args)
            except Exception as exc:
                result = f"TOOL_ERROR: {exc}"
            result = str(result)[: min(6_000, self.max_observation_chars)]
            total_chars += len(result)
            if total_chars > self.max_observation_chars:
                return DeveloperExplorationResult(
                    "fallback",
                    observations=observations,
                    model_calls=model_calls,
                    model_turns=model_turns,
                    model_used=last_model,
                    reason="exploration_context_budget_exhausted",
                )
            observation = ToolObservation(step, name, args, result)
            observations.append(observation)
            # Conserver explicitement l'action précédente aide les modèles qui ont
            # besoin du couple action/observation pour raisonner sur plusieurs tours.
            messages.append({
                "role": "assistant",
                "content": json.dumps(action, ensure_ascii=False, sort_keys=True),
            })
            messages.append({
                "role": "user",
                "content": explorer_observation_prompt(
                    step=step, action=name, args=args, result=result
                ),
            })

        return DeveloperExplorationResult(
            "fallback",
            observations=observations,
            model_calls=model_calls,
            model_turns=model_turns,
            model_used=last_model,
            reason="exploration_step_budget_exhausted",
        )

    def _initial_context(self, task: str, approved: list[str], tests: list[str]) -> str:
        relevant = self.repo_intelligence.relevant_files(task, limit=8)
        facts: list[str] = []
        for info in relevant:
            symbols = ", ".join(item.name for item in info.symbols[:12]) or "none"
            facts.append(f"- {info.path}: symbols={symbols}")
        docs_tools = "- memory_hints(query) [leçons historiques assainies, non autoritaires]\n"
        if self.docs_explorer is not None:
            docs_tools += (
                "- search_docs(query) [uniquement domaines officiels autorisés: "
                + ", ".join(self.docs_domains)
                + "]\n- read_docs(url) [URL de ces domaines uniquement]"
            )
        return explorer_initial_prompt(
            task=task,
            approved=approved,
            tests=tests,
            repo_facts=facts,
            docs_tools=docs_tools,
        )

    @staticmethod
    def _schema() -> dict[str, Any]:
        # Les backends OpenAI-like ne respectent pas tous JSON Schema au même niveau ;
        # garder un objet plat améliore la compatibilité.
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "query": {"type": "string"},
                "path": {"type": "string"},
                "url": {"type": "string"},
                "symbol": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "brief": {"type": "string"},
                "reason": {"type": "string"},
                "recommended_files": {"type": "array", "items": {"type": "string"}},
                "recommended_tests": {"type": "array", "items": {"type": "string"}},
                "file_instructions": {"type": "object"},
            },
            "required": ["action"],
        }

    @staticmethod
    def _parse_action(response: Any) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise ValueError("Réponse d'exploration non structurée.")
        message = response.get("message")
        if not isinstance(message, dict):
            raise ValueError("Message d'exploration absent.")
        payload = message.get("content", "")
        if isinstance(payload, dict):
            data = payload
        elif isinstance(payload, str):
            text = payload.strip()
            if text.startswith("```json"):
                text = text.split("```json", 1)[1].split("```", 1)[0].strip()
            elif text.startswith("```"):
                text = text.split("```", 1)[1].split("```", 1)[0].strip()
            data = json.loads(text)
        else:
            raise ValueError("Contenu d'exploration invalide.")
        if not isinstance(data, dict):
            raise ValueError("Action d'exploration invalide.")
        return data

    def _bounded_arguments(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action in {"search_code", "find_symbol", "search_docs", "memory_hints"}:
            query = str(payload.get("query") or "").strip()[:240]
            if not query:
                raise ValueError("query vide")
            if action == "search_docs" and self.docs_explorer is None:
                raise ValueError("documentation web non autorisée pour cette tâche")
            return {"query": query}
        if action == "read_docs":
            if self.docs_explorer is None:
                raise ValueError("documentation web non autorisée pour cette tâche")
            url = str(payload.get("url") or "").strip()[:2000]
            if not url:
                raise ValueError("url vide")
            return {"url": url}
        if action in {"read_file", "inspect_file", "dependencies"}:
            path = self._validate_existing_path(payload.get("path"))
            args: dict[str, Any] = {"path": path}
            if action == "read_file":
                start = max(1, min(int(payload.get("start_line") or 1), 100_000))
                end = max(start, min(int(payload.get("end_line") or (start + 120)), start + 199))
                args.update(start_line=start, end_line=end)
            return args
        if action == "read_symbol":
            path = self._validate_existing_path(payload.get("path"))
            symbol = str(payload.get("symbol") or "").strip()
            if not _SAFE_QUALIFIED_SYMBOL.fullmatch(symbol):
                raise ValueError("symbole invalide")
            return {"path": path, "symbol": symbol}
        if action == "references_to":
            symbol = str(payload.get("symbol") or "").strip()
            if not _SAFE_SYMBOL.fullmatch(symbol):
                raise ValueError("symbole invalide")
            return {"symbol": symbol}
        if action == "find_tests":
            raw_paths = payload.get("paths") or ([payload.get("path")] if payload.get("path") else [])
            paths = [self._validate_existing_path(item) for item in list(raw_paths)[:8]]
            return {"paths": paths}
        raise ValueError(f"outil non supporté: {action}")

    def _execute(self, action: str, args: dict[str, Any]) -> str:
        intel = self.repo_intelligence
        if action == "memory_hints":
            hints = self.engineering_memory.relevant_hints(args["query"], limit=4)
            return json.dumps([hint.to_dict() for hint in hints], ensure_ascii=False)
        if action == "search_docs":
            if self.docs_explorer is None:
                raise ValueError("documentation web non autorisée")
            return self.docs_explorer.search_json(args["query"], limit=5)
        if action == "read_docs":
            if self.docs_explorer is None:
                raise ValueError("documentation web non autorisée")
            return json.dumps(self.docs_explorer.read(args["url"]), ensure_ascii=False)
        if action == "search_code":
            hits = intel.search_code(args["query"], limit=12)
            return json.dumps([asdict(hit) for hit in hits], ensure_ascii=False)
        if action == "find_symbol":
            hits = intel.find_symbols(args["query"], limit=16)
            return json.dumps([asdict(hit) for hit in hits], ensure_ascii=False)
        if action == "read_symbol":
            return intel.read_symbol(args["path"], args["symbol"], max_chars=8_000)
        if action == "read_file":
            return intel.read_file(args["path"], start_line=args["start_line"], end_line=args["end_line"], max_chars=8_000)
        if action == "inspect_file":
            return json.dumps(intel.inspect_file(args["path"]).to_dict(), ensure_ascii=False)
        if action == "find_tests":
            return json.dumps(intel.find_tests_for(args["paths"], limit=12), ensure_ascii=False)
        if action == "references_to":
            hits = intel.references_to(args["symbol"], limit=15)
            return json.dumps([asdict(hit) for hit in hits], ensure_ascii=False)
        if action == "dependencies":
            return json.dumps(intel.dependency_neighbors(args["path"], limit=16), ensure_ascii=False)
        raise ValueError(f"outil inconnu: {action}")

    def _finish(
        self,
        action: str,
        payload: dict[str, Any],
        approved_rel: list[str],
        observations: list[ToolObservation],
        model_calls: int,
        model_turns: int,
        model_used: str | None,
    ) -> DeveloperExplorationResult:
        brief = str(payload.get("brief") or payload.get("reason") or "").strip()[:8_000]
        recommended_files = self._safe_recommendations(payload.get("recommended_files") or [])
        recommended_tests = [
            path for path in self._safe_recommendations(payload.get("recommended_tests") or [])
            if Path(path).name.startswith("test_") or "/test_" in f"/{path}"
        ]
        raw_instructions = payload.get("file_instructions") or {}
        file_instructions: dict[str, str] = {}
        if isinstance(raw_instructions, dict):
            approved_set = set(approved_rel)
            for raw_path, instruction in raw_instructions.items():
                rel = self._relative(raw_path, require_editable=True)
                if rel in approved_set and isinstance(instruction, str) and instruction.strip():
                    file_instructions[rel] = instruction.strip()[:4_000]

        if action == "proceed":
            if not brief:
                brief = "Implémenter la tâche en respectant les faits observés dans le repository."
            return DeveloperExplorationResult(
                "proceed",
                brief=brief,
                file_instructions=file_instructions,
                recommended_files=recommended_files,
                recommended_tests=recommended_tests,
                observations=observations,
                model_calls=model_calls,
                model_turns=model_turns,
                model_used=model_used,
                reason=str(payload.get("reason") or "repo_exploration_completed")[:2_000],
            )

        if not recommended_files:
            return DeveloperExplorationResult(
                "fallback",
                observations=observations,
                model_calls=model_calls,
                model_turns=model_turns,
                model_used=model_used,
                reason="replan_without_recommended_files",
            )
        return DeveloperExplorationResult(
            "replan",
            brief=brief,
            file_instructions=file_instructions,
            recommended_files=recommended_files,
            recommended_tests=recommended_tests,
            observations=observations,
            model_calls=model_calls,
            model_turns=model_turns,
            model_used=model_used,
            reason=str(payload.get("reason") or "planner_targets_mismatch_repository")[:2_000],
        )

    def _safe_recommendations(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        results: list[str] = []
        for value in values[:12]:
            rel = self._relative(value, require_editable=True)
            if rel and rel not in results:
                results.append(rel)
        return results

    def _validate_existing_path(self, raw: Any) -> str:
        rel = self._relative(raw)
        if not rel or not (self.repo_root / rel).is_file():
            raise ValueError(f"fichier non autorisé/introuvable: {raw}")
        return rel

    def _relative(self, raw: Any, *, require_editable: bool = False) -> str | None:
        if not isinstance(raw, (str, Path)):
            return None
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            resolved = (self.repo_root / candidate).resolve(strict=False)
        try:
            rel = resolved.relative_to(self.repo_root).as_posix()
        except ValueError:
            return None
        # Lecture et écriture ont des capacités distinctes : le Trusted Computing
        # Base reste lisible pour raisonner, mais ne peut jamais être recommandé
        # comme cible d'écriture par le modèle.
        if require_editable:
            return rel if is_agent_editable_path(rel) else None
        return rel if is_agent_readable_path(rel) else None
