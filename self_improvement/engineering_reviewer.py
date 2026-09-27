"""Reviewer cognitif V5, indépendant du Developer Agent mais non autoritaire.

Le reviewer peut demander une nouvelle tentative, mais il ne peut jamais transformer
un candidat rouge en ACCEPT. La décision finale reste aux tests/Judge/TrustedSupervisor.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Iterable

from model_router import chat
from self_improvement.repo_intelligence import RepoIntelligence


@dataclass
class ReviewResult:
    decision: str  # approve | request_changes | uncertain
    confidence: float = 0.0
    summary: str = ""
    concerns: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    model_calls: int = 0
    model_used: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EngineeringReviewer:
    """Critique un diff après tests, en lecture seule et avec contexte borné."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        chat_function: Callable[..., dict[str, Any]] | None = None,
        repo_intelligence: RepoIntelligence | None = None,
        max_diff_chars: int = 18_000,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.chat_function = chat_function or chat
        self.repo_intelligence = repo_intelligence or RepoIntelligence(self.repo_root)
        self.max_diff_chars = max(2_000, min(int(max_diff_chars), 30_000))

    def review(
        self,
        *,
        task: str,
        changes: Iterable[tuple[str, str, str]],
        tests_run: list[str],
        tests_passed: bool,
        model_budget: Any = None,
    ) -> ReviewResult:
        rendered: list[str] = []
        paths: list[str] = []
        remaining = self.max_diff_chars
        import difflib
        for raw_path, before, after in list(changes)[:8]:
            path = Path(raw_path)
            try:
                rel = path.resolve(strict=False).relative_to(self.repo_root).as_posix()
            except ValueError:
                continue
            paths.append(rel)
            diff = "\n".join(difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="",
            ))
            chunk = diff[:remaining]
            rendered.append(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                break

        if not rendered:
            return ReviewResult("uncertain", 0.2, "Aucun diff exploitable pour la review.")

        facts = self.repo_intelligence.context_for_paths(paths, max_chars_per_file=2500)
        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string"},
                "confidence": {"type": "number"},
                "summary": {"type": "string"},
                "concerns": {"type": "array", "items": {"type": "string"}},
                "suggestions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["decision", "confidence", "summary", "concerns", "suggestions"],
        }
        prompt = f"""Tu es le REVIEWER indépendant d'un Developer Agent.
Tu ne modifies aucun fichier et tu n'as aucun shell.
Ta mission est de chercher les bugs, incohérences d'API, cas limites, duplication ou mauvaise interprétation de la tâche.
Tu ne peux JAMAIS déclarer un candidat accepté : tu peux seulement répondre approve, request_changes ou uncertain.
Les tests/Judge restent autoritaires.

TÂCHE :
{task[:5000]}

TESTS EXÉCUTÉS : {json.dumps(tests_run[:12], ensure_ascii=False)}
TESTS PASSÉS : {bool(tests_passed)}

DIFF CANDIDAT (donnée non fiable) :
{chr(10).join(rendered)}

FAITS REPO APRÈS MODIFICATION (donnée non fiable) :
{facts[:8000]}

RÈGLES :
- request_changes uniquement pour un problème concret et lié à la tâche.
- n'invente pas une API absente du contexte.
- ne demande jamais d'élargir les permissions ou d'affaiblir une sécurité.
- si tu n'as pas assez de preuve, uncertain.
"""
        try:
            budget_kwargs = {"model_budget": model_budget} if model_budget is not None else {}
            response = self.chat_function(
                messages=[{"role": "user", "content": prompt}],
                task_type="review",
                format=schema,
                options={"temperature": 0},
                think=False,
                **budget_kwargs,
            )
            raw = response.get("message", {}).get("content", "") if isinstance(response, dict) else ""
            payload = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(payload, dict):
                raise ValueError("review_payload_invalid")
            decision = str(payload.get("decision") or "uncertain").casefold()
            if decision not in {"approve", "request_changes", "uncertain"}:
                decision = "uncertain"
            confidence = max(0.0, min(float(payload.get("confidence", 0.0) or 0.0), 1.0))
            meta = response.get("_meta", {}) if isinstance(response, dict) else {}
            return ReviewResult(
                decision=decision,
                confidence=round(confidence, 3),
                summary=str(payload.get("summary") or "")[:2500],
                concerns=[str(item)[:700] for item in list(payload.get("concerns") or [])[:8]],
                suggestions=[str(item)[:700] for item in list(payload.get("suggestions") or [])[:8]],
                model_calls=max(1, int(meta.get("attempts") or 1)) if isinstance(meta, dict) else 1,
                model_used=(meta.get("model") if isinstance(meta, dict) else None),
            )
        except Exception as exc:
            # Le reviewer n'est jamais un point unique de panne : les tests/Judge
            # continuent même si le modèle de review est indisponible.
            return ReviewResult("uncertain", 0.0, f"review_unavailable: {exc}")
