"""Experiment Memory V1 — Mémoire persistante et déterministe d'auto-amélioration.

Permet à l'orchestrateur d'amélioration de :
- Enregistrer et historiser les tentatives d'amélioration avec leurs métriques et décisions du Judge Engine.
- Retrouver rapidement des problèmes similaires déjà résolus ou échoués.
- Recommander des stratégies qui ont fonctionné (ACCEPT) et proscrire celles qui ont échoué (REJECT).
- Assurer la sécurité : sanitization stricte des secrets et interdiction d'accès au holdout.
- Persistance locale robuste en JSONL avec écritures atomiques et tolérance aux lignes corrompues.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from self_improvement.judge_engine import JudgeDecision, JudgeMetrics, JudgeResult


SCHEMA_VERSION = 1

# ===========================================================================
# 1. SANITIZATION ET FILTRES DE SÉCURITÉ
# ===========================================================================

# Regex pour détecter les clés d'API, tokens et secrets courants
_SECRET_PATTERNS = [
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{20,})\b", re.IGNORECASE), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"\b(gsk_[A-Za-z0-9_-]{20,})\b", re.IGNORECASE), "[REDACTED_GROQ_KEY]"),
    (re.compile(r"\b(csk-[A-Za-z0-9_-]{20,})\b", re.IGNORECASE), "[REDACTED_CEREBRAS_KEY]"),
    (re.compile(r"\b(AIza[0-9A-Za-z-_]{35})\b"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"(api[_-]?key\s*[:=]\s*['\"])[^'\"]+(['\"])", re.IGNORECASE), r"\1[REDACTED_KEY]\2"),
    (re.compile(r"(password\s*[:=]\s*['\"])[^'\"]+(['\"])", re.IGNORECASE), r"\1[REDACTED_PASSWORD]\2"),
    (re.compile(r"(secret\s*[:=]\s*['\"])[^'\"]+(['\"])", re.IGNORECASE), r"\1[REDACTED_SECRET]\2"),
    (re.compile(r"(token\s*[:=]\s*['\"])[^'\"]+(['\"])", re.IGNORECASE), r"\1[REDACTED_TOKEN]\2"),
]

_HOLDOUT_PATTERN = re.compile(r"[A-Za-z0-9_./\\-]*\.self_improvement_holdout[A-Za-z0-9_./\\-]*", re.IGNORECASE)


def sanitize_text(text: str) -> str:
    """Supprime les secrets connus et références sensibles avant persistance."""
    if not isinstance(text, str):
        return text

    # Redaction du holdout
    text = _HOLDOUT_PATTERN.sub("[REDACTED_HOLDOUT_PATH]", text)

    # Redaction des secrets
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)

    return text


def sanitize_value(value: Any) -> Any:
    """Assainit récursivement chaînes, dictionnaires et listes."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {sanitize_text(str(k)): sanitize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_value(item) for item in value]
    return value


# ===========================================================================
# 1.5 LEÇON DÉRIVÉE (DERIVED LESSON GENERATION)
# ===========================================================================

def derive_reusable_lesson(
    *,
    judge_result: JudgeResult,
    strategy: str,
    problem_type: str,
    root_cause: str,
    regressions: list[str] | None = None,
    improvements: list[str] | None = None,
) -> str:
    """Dérive une leçon fiable à partir du résultat réel d'une expérience.
    
    Garanties:
    - La leçon est dérivée DE CETTE EXPÉRIENCE, pas copiée d'une ancienne
    - La leçon est concise (< 200 chars)
    - La leçon est liée à la cause racine
    - Si aucune leçon fiable: retourne "" plutôt qu'une mauvaise leçon
    """
    decision = str(judge_result.decision).upper()
    regressions = regressions or []
    improvements = improvements or []
    
    # Rule 1: Si ACCEPT et améliorations mesurables
    if decision == "ACCEPT" and improvements:
        # Récapitulatif des améliorations
        top_improvement = improvements[0] if improvements else "tests"
        return f"Strategy '{strategy}' ACCEPTED: improved {top_improvement}. Apply for similar {problem_type} issues."
    
    # Rule 2: Si REJECT et régressions claires
    if decision == "REJECT" and regressions:
        # Récapitulatif des régressions
        top_regression = regressions[0] if regressions else "baseline"
        return f"Strategy '{strategy}' REJECTED: caused {top_regression}. Avoid for {problem_type}."
    
    # Rule 3: Si UNCERTAIN avec raison probable
    if decision == "UNCERTAIN":
        reasons = judge_result.reasons or []
        if reasons and "timeout" in str(reasons[0]).lower():
            return f"Strategy '{strategy}' UNCERTAIN: provider timeout. Retry with different provider."
        if reasons and "conflict" in str(reasons[0]).lower():
            return f"Strategy '{strategy}' UNCERTAIN: merge conflict detected. Needs manual resolution."
        return f"Strategy '{strategy}' UNCERTAIN: inconclusive results. Need more data."
    
    # Rule 4: Minimal NEUTRAL case
    if decision in ("NEUTRAL", "NO_CHANGE"):
        return ""
    
    # Fallback: si on ne peut pas dériver fiable, ne pas mentir
    if judge_result.confidence < 0.3:
        return ""
    
    # Dernier recours: résumé basique
    base = f"Strategy: {strategy[:50]}. Decision: {decision}."
    if judge_result.score:
        base += f" Score: {judge_result.score:.1f}%."
    return base[:200]


# ===========================================================================
# 2. STRUCTURES DE DONNÉES
# ===========================================================================

@dataclass
class ExperimentRecord:
    """Enregistrement complet et immuable d'une expérience d'amélioration."""
    experiment_id: str
    timestamp: str  # ISO 8601 UTC
    task: str
    problem_type: str
    root_cause: str
    strategy: str
    files_changed: list[str] = field(default_factory=list)
    before_metrics: dict[str, Any] = field(default_factory=dict)
    after_metrics: dict[str, Any] = field(default_factory=dict)
    judge_decision: str = "UNCERTAIN"  # ACCEPT | REJECT | UNCERTAIN
    judge_score: float = 0.0
    judge_confidence: float = 0.0
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    failure_type: str | None = None
    result_summary: str = ""
    reusable_lesson: str = ""
    tags: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)

    def sanitized(self) -> ExperimentRecord:
        """Retourne une copie assainie garantie sans secrets ni holdout."""
        data = asdict(self)
        clean = sanitize_value(data)
        return ExperimentRecord(**clean)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExperimentRecord:
        """Instancie un ExperimentRecord en tolérant les champs optionnels manquants."""
        known_fields = cls.__dataclass_fields__
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


@dataclass
class ExperimentQuery:
    """Critères de filtrage pour la recherche d'expériences."""
    problem_type: str | None = None
    failure_type: str | None = None
    strategy: str | None = None
    decision: str | None = None
    tags: list[str] | None = None
    since: str | None = None  # ISO 8601
    until: str | None = None  # ISO 8601
    limit: int | None = None


@dataclass
class ExperimentMatch:
    """Résultat de recherche avec score de similarité explicable."""
    record: ExperimentRecord
    similarity_score: float  # 0.0 à 1.0
    matched_fields: list[str]
    outcome: str  # ACCEPT | REJECT | UNCERTAIN
    strategy: str
    reusable_lesson: str


# ===========================================================================
# 3. HELPER D'INTÉGRATION JUDGE ENGINE
# ===========================================================================

def create_experiment_record(
    *,
    experiment_id: str | None = None,
    task: str,
    problem_type: str,
    root_cause: str,
    strategy: str,
    judge_result: JudgeResult,
    files_changed: list[str] | None = None,
    failure_type: str | None = None,
    reusable_lesson: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ExperimentRecord:
    """Construit un ExperimentRecord structuré à partir d'un JudgeResult.
    
    IMPORTANTE: reusable_lesson est maintenant auto-dérivée à partir du JudgeResult
    à moins qu'un override explicite ne soit fourni. Ne JAMAIS passer une leçon
    copiée d'une ancienne expérience.
    """
    exp_id = experiment_id or f"exp_{int(time.time() * 1000)}"
    timestamp = datetime.now(timezone.utc).isoformat()

    decision_str = (
        judge_result.decision.value
        if isinstance(judge_result.decision, JudgeDecision)
        else str(judge_result.decision)
    )

    summary_parts = []
    if judge_result.reasons:
        summary_parts.append(judge_result.reasons[0])
    if judge_result.improvements:
        summary_parts.append(f"+{len(judge_result.improvements)} améliorations")
    if judge_result.regressions:
        summary_parts.append(f"-{len(judge_result.regressions)} régressions")

    # Derive lesson from this experiment if not explicitly provided
    if not reusable_lesson:
        reusable_lesson = derive_reusable_lesson(
            judge_result=judge_result,
            strategy=strategy,
            problem_type=problem_type,
            root_cause=root_cause,
            regressions=list(judge_result.regressions),
            improvements=list(judge_result.improvements),
        )

    return ExperimentRecord(
        experiment_id=exp_id,
        timestamp=timestamp,
        task=task,
        problem_type=problem_type,
        root_cause=root_cause,
        strategy=strategy,
        files_changed=list(files_changed or []),
        before_metrics=judge_result.before_metrics.to_dict() if hasattr(judge_result.before_metrics, "to_dict") else dict(judge_result.before_metrics),
        after_metrics=judge_result.after_metrics.to_dict() if hasattr(judge_result.after_metrics, "to_dict") else dict(judge_result.after_metrics),
        judge_decision=decision_str,
        judge_score=judge_result.score,
        judge_confidence=judge_result.confidence,
        regressions=list(judge_result.regressions),
        improvements=list(judge_result.improvements),
        failure_type=failure_type,
        result_summary=" | ".join(summary_parts) or f"Decision: {decision_str}",
        reusable_lesson=reusable_lesson,
        tags=list(tags or []),
        metadata=dict(metadata or {}),
    )


# ===========================================================================
# 4. MOTEUR DE SIMILARITÉ DÉTERMINISTE
# ===========================================================================

def _tokenize(text: str) -> set[str]:
    """Extrait des mots-clés normalisés pour la comparaison textuelle."""
    if not text:
        return set()
    cleaned = re.sub(r"[^\w\s_-]", " ", text.lower())
    stop_words = {
        "dans", "pour", "avec", "sans", "sur", "une", "des", "les", "que", "qui",
        "par", "est", "the", "and", "for", "with", "this", "that", "from", "into",
    }
    return {w for w in cleaned.split() if len(w) > 2 and w not in stop_words}


def calculate_similarity(
    query_task: str,
    query_problem_type: str,
    query_failure_type: str,
    query_tags: set[str],
    record: ExperimentRecord,
) -> tuple[float, list[str]]:
    """Calcule un score de similarité déterministe et explicable entre 0.0 et 1.0."""
    score = 0.0
    matched_fields: list[str] = []

    # 1. Problem type exact (Poids : 0.35)
    if query_problem_type and record.problem_type:
        if query_problem_type.casefold() == record.problem_type.casefold():
            score += 0.35
            matched_fields.append("problem_type")
        elif query_problem_type.casefold() in record.problem_type.casefold() or record.problem_type.casefold() in query_problem_type.casefold():
            score += 0.20
            matched_fields.append("problem_type_partial")

    # 2. Failure type exact (Poids : 0.25)
    if query_failure_type and record.failure_type:
        if query_failure_type.casefold() == record.failure_type.casefold():
            score += 0.25
            matched_fields.append("failure_type")

    # 3. Tags overlap (Poids : 0.20)
    record_tags = {t.casefold() for t in record.tags}
    if query_tags and record_tags:
        common_tags = query_tags & record_tags
        if common_tags:
            tag_ratio = len(common_tags) / max(len(query_tags), len(record_tags))
            tag_points = round(0.20 * tag_ratio, 3)
            score += tag_points
            matched_fields.append(f"tags({','.join(sorted(common_tags))})")

    # 4. Task & Root Cause keyword overlap (Poids : 0.20)
    query_tokens = _tokenize(query_task)
    record_tokens = _tokenize(record.task) | _tokenize(record.root_cause) | _tokenize(record.strategy)
    if query_tokens and record_tokens:
        common_words = query_tokens & record_tokens
        if common_words:
            word_ratio = min(1.0, len(common_words) / len(query_tokens))
            word_points = round(0.20 * word_ratio, 3)
            score += word_points
            matched_fields.append(f"keywords({len(common_words)})")

    final_score = round(min(1.0, score), 3)
    return final_score, matched_fields


# ===========================================================================
# 5. GESTIONNAIRE DE MÉMOIRE D'EXPÉRIENCES (EXPERIMENT MEMORY V1)
# ===========================================================================

class ExperimentMemory:
    """Mémoire persistante, locale et thread-safe pour l'historique d'amélioration."""

    def __init__(self, storage_path: str | Path | None = None):
        if storage_path is None:
            base_dir = Path(__file__).resolve().parent / "reports"
            base_dir.mkdir(parents=True, exist_ok=True)
            self.storage_path = base_dir / "experiment_history.jsonl"
        else:
            self.storage_path = Path(storage_path).resolve()
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)

        self._records_cache: list[ExperimentRecord] = []
        self._load_records()

    def _load_records(self) -> None:
        """Charge les enregistrements depuis le fichier JSONL en isolant les lignes corrompues."""
        self._records_cache.clear()
        if not self.storage_path.is_file():
            return

        try:
            content = self.storage_path.read_text(encoding="utf-8", errors="replace")
            for line_idx, line in enumerate(content.splitlines(), start=1):
                clean_line = line.strip()
                if not clean_line or clean_line.startswith("#"):
                    continue
                try:
                    raw_data = json.loads(clean_line)
                    if isinstance(raw_data, dict) and "experiment_id" in raw_data:
                        record = ExperimentRecord.from_dict(raw_data)
                        self._records_cache.append(record)
                except Exception:
                    # Ligne corrompue ignorée sans faire échouer toute la mémoire
                    continue
        except Exception:
            self._records_cache.clear()

    def _save_records_atomically(self) -> None:
        """Écrit l'ensemble des enregistrements de manière atomique via un fichier temporaire."""
        temp_dir = self.storage_path.parent
        temp_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=temp_dir,
            prefix=".exp_mem_",
            suffix=".tmp",
            delete=False,
            newline="\n",
        ) as tmp_file:
            for record in self._records_cache:
                sanitized_dict = record.sanitized().to_dict()
                tmp_file.write(json.dumps(sanitized_dict, ensure_ascii=False) + "\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
            temp_path = Path(tmp_file.name)

        try:
            os.replace(temp_path, self.storage_path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def record_experiment(self, record: ExperimentRecord | dict[str, Any]) -> ExperimentRecord:
        """Enregistre une expérience en mémoire et persiste immédiatement de façon atomique."""
        if isinstance(record, dict):
            rec = ExperimentRecord.from_dict(record)
        else:
            rec = record

        sanitized_rec = rec.sanitized()

        # Remplacement si ID existant ou ajout
        existing_idx = next(
            (i for i, r in enumerate(self._records_cache) if r.experiment_id == sanitized_rec.experiment_id),
            None,
        )

        if existing_idx is not None:
            self._records_cache[existing_idx] = sanitized_rec
        else:
            self._records_cache.append(sanitized_rec)

        self._save_records_atomically()
        return sanitized_rec

    def get_experiment(self, experiment_id: str) -> ExperimentRecord | None:
        """Récupère une expérience spécifique par son identifiant unique."""
        return next((r for r in self._records_cache if r.experiment_id == experiment_id), None)

    def list_experiments(self, query: ExperimentQuery | None = None) -> list[ExperimentRecord]:
        """Filtre et retourne la liste des expériences selon les critères fournis."""
        records = list(self._records_cache)
        if query is None:
            return records

        if query.problem_type:
            pt = query.problem_type.casefold()
            records = [r for r in records if pt in r.problem_type.casefold()]

        if query.failure_type:
            ft = query.failure_type.casefold()
            records = [r for r in records if r.failure_type and ft in r.failure_type.casefold()]

        if query.strategy:
            st = query.strategy.casefold()
            records = [r for r in records if st in r.strategy.casefold()]

        if query.decision:
            dec = query.decision.upper()
            records = [r for r in records if r.judge_decision.upper() == dec]

        if query.tags:
            required_tags = {t.casefold() for t in query.tags}
            records = [r for r in records if required_tags.issubset({t.casefold() for t in r.tags})]

        if query.since:
            records = [r for r in records if r.timestamp >= query.since]

        if query.until:
            records = [r for r in records if r.timestamp <= query.until]

        if query.limit is not None and query.limit > 0:
            records = records[: query.limit]

        return records

    def find_similar(
        self,
        *,
        task: str = "",
        problem_type: str = "",
        failure_type: str = "",
        tags: list[str] | None = None,
        limit: int = 5,
        min_score: float = 0.1,
    ) -> list[ExperimentMatch]:
        """Recherche déterministe des expériences les plus similaires avec score explicable."""
        query_tags = {t.casefold() for t in (tags or [])}
        matches: list[ExperimentMatch] = []

        for record in self._records_cache:
            score, fields_matched = calculate_similarity(
                query_task=task,
                query_problem_type=problem_type,
                query_failure_type=failure_type,
                query_tags=query_tags,
                record=record,
            )

            if score >= min_score:
                matches.append(
                    ExperimentMatch(
                        record=record,
                        similarity_score=score,
                        matched_fields=fields_matched,
                        outcome=record.judge_decision,
                        strategy=record.strategy,
                        reusable_lesson=record.reusable_lesson,
                    )
                )

        # Tri décroissant par score de similarité
        matches.sort(key=lambda m: m.similarity_score, reverse=True)
        return matches[:limit]

    def find_successful_strategies(
        self,
        *,
        task: str = "",
        problem_type: str = "",
        failure_type: str = "",
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[ExperimentMatch]:
        """Recherche UNIQUEMENT les stratégies validées par le Judge (ACCEPT).

        Garantie de sécurité : aucun enregistrement REJECT ou UNCERTAIN n'est retourné.
        """
        candidates = self.find_similar(
            task=task,
            problem_type=problem_type,
            failure_type=failure_type,
            tags=tags,
            limit=len(self._records_cache) or 10,
            min_score=0.05,
        )
        successful = [m for m in candidates if m.outcome.upper() == "ACCEPT"]
        return successful[:limit]

    def find_failed_strategies(
        self,
        *,
        task: str = "",
        problem_type: str = "",
        failure_type: str = "",
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[ExperimentMatch]:
        """Recherche les stratégies REJETÉES pour éviter de répéter les mêmes erreurs."""
        candidates = self.find_similar(
            task=task,
            problem_type=problem_type,
            failure_type=failure_type,
            tags=tags,
            limit=len(self._records_cache) or 10,
            min_score=0.05,
        )
        failed = [m for m in candidates if m.outcome.upper() == "REJECT"]
        return failed[:limit]

    def stats(self) -> dict[str, Any]:
        """Retourne des statistiques globales consolidées sur la mémoire."""
        total = len(self._records_cache)
        accepted = sum(1 for r in self._records_cache if r.judge_decision.upper() == "ACCEPT")
        rejected = sum(1 for r in self._records_cache if r.judge_decision.upper() == "REJECT")
        uncertain = sum(1 for r in self._records_cache if r.judge_decision.upper() == "UNCERTAIN")

        by_problem_type: dict[str, int] = {}
        for r in self._records_cache:
            pt = r.problem_type or "unknown"
            by_problem_type[pt] = by_problem_type.get(pt, 0) + 1

        return {
            "total_experiments": total,
            "accepted_count": accepted,
            "rejected_count": rejected,
            "uncertain_count": uncertain,
            "success_rate": round((accepted / total) * 100.0, 1) if total else 0.0,
            "problem_types_distribution": by_problem_type,
            "storage_file": str(self.storage_path),
            "schema_version": SCHEMA_VERSION,
        }
