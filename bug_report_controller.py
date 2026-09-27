"""Capture explicite et confidentielle des mauvaises réponses observées dans la GUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import unicodedata

from self_improvement.real_bug_corpus import BugCorpusError, RealBugCase, RealBugCorpus, sanitize_for_storage


MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_CHARS = 4_000
MAX_COMMENT_CHARS = 2_000
MAX_EXPECTED_CHARS = 2_000
MAX_REVIEW_CRITERION_CHARS = 300
MAX_REVIEW_CRITERIA = 20


@dataclass(frozen=True)
class BugReportDraft:
    user_message: str
    assistant_response: str
    conversation_history: list[dict[str, str]]
    session_context: dict
    active_attachment: dict | None


def _normalized(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value).casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _category(user_message: str, assistant_response: str) -> str:
    text = _normalized(f"{user_message} {assistant_response}")
    if any(term in text for term in ("piece jointe", "fichier joint", "document joint")):
        return "compréhension contextuelle"
    if any(term in text for term in ("fichier", "dossier", "bureau", "documents")):
        return "actions fichiers"
    return "retour utilisateur GUI"


class BugReportController:
    """Prépare, enregistre et soumet à revue les signalements explicites."""

    def __init__(self, corpus=None, *, now=None):
        self.corpus = corpus or RealBugCorpus()
        self._now = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def prepare(*, user_message, assistant_response, history=None, session_context=None) -> BugReportDraft:
        if not isinstance(user_message, str) or not user_message.strip():
            raise BugCorpusError("Le message utilisateur associé est obligatoire.")
        if not isinstance(assistant_response, str) or not assistant_response.strip():
            raise BugCorpusError("La réponse assistant associée est obligatoire.")
        clean_history = []
        used = 0
        for item in list(history or [])[-MAX_HISTORY_MESSAGES:]:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            content = str(item.get("content", ""))
            remaining = MAX_HISTORY_CHARS - used
            if remaining <= 0:
                break
            content = content[:remaining]
            used += len(content)
            clean_history.append({"role": item["role"], "content": content})
        session = dict(session_context or {})
        raw_attachment = session.pop("active_attachment", None)
        attachment = None
        if isinstance(raw_attachment, dict):
            # Le chemin/reference local n'est pas nécessaire pour reproduire le bug.
            attachment = {
                key: raw_attachment[key]
                for key in ("name", "format", "size")
                if key in raw_attachment
            }
            attachment["source"] = "gui"
            session["active_attachment"] = attachment
        allowed_context = {
            key: session.get(key)
            for key in ("last_intent", "last_file", "last_folder", "last_action", "active_attachment")
            if session.get(key) is not None
        }
        payload = sanitize_for_storage({
            "user_message": user_message.strip(),
            "assistant_response": assistant_response.strip(),
            "conversation_history": clean_history,
            "session_context": allowed_context,
            "active_attachment": attachment,
        })
        return BugReportDraft(**payload)

    def _feedback_duplicate(self, draft: BugReportDraft) -> bool:
        signature = (
            _normalized(draft.user_message),
            _normalized(draft.assistant_response),
            bool(draft.active_attachment),
        )
        return any(
            (
                _normalized(case.user_message),
                _normalized(case.incorrect_response),
                case.has_attachment,
            ) == signature
            for case in self.corpus.load()
        )

    def submit(self, draft: BugReportDraft, *, incorrect_comment: str, expected_behavior="") -> RealBugCase:
        comment = str(incorrect_comment or "").strip()
        expected = str(expected_behavior or "").strip()
        if not comment:
            raise BugCorpusError("Expliquez ce qui était incorrect avant d'enregistrer.")
        if self._feedback_duplicate(draft):
            raise BugCorpusError("Cette réponse a déjà été signalée dans un contexte équivalent.")
        now = self._now()
        digest = hashlib.sha256(
            f"{_normalized(draft.user_message)}|{_normalized(draft.assistant_response)}|{bool(draft.active_attachment)}".encode("utf-8")
        ).hexdigest()[:12]
        category = _category(draft.user_message, draft.assistant_response)
        case = RealBugCase(
            id=f"gui-{now:%Y%m%d}-{digest}",
            created_at=now.astimezone(timezone.utc).isoformat(),
            status="new",
            category=category,
            conversation_history=draft.conversation_history,
            user_message=draft.user_message,
            incorrect_response=draft.assistant_response,
            expected_behavior={
                "description": expected[:MAX_EXPECTED_CHARS] or comment[:MAX_COMMENT_CHARS],
                "user_comment": comment[:MAX_COMMENT_CHARS],
                "criteria": [],
            },
            session_context=draft.session_context,
            has_attachment=bool(draft.active_attachment),
            family_id=f"gui-feedback-{re.sub(r'[^a-z0-9]+', '-', _normalized(category)).strip('-')}",
            variants=[],
            source="gui",
            tags=["user-reported", "needs-review"],
        )
        return self.corpus.add(case)

    def list_new(self):
        cases = [case.sanitized() for case in self.corpus.list_new()]
        print(f"[BugReview] {len(cases)} signalements à revoir")
        return cases

    def get_for_review(self, bug_id: str):
        return self.corpus.get(bug_id).sanitized()

    @staticmethod
    def normalize_review_criteria(criteria) -> list[str]:
        if isinstance(criteria, str):
            values = criteria.splitlines()
        elif isinstance(criteria, (list, tuple)):
            values = list(criteria)
        else:
            raise BugCorpusError("Les critères attendus doivent être des chaînes.")
        normalized = []
        seen = set()
        for value in values:
            if not isinstance(value, str):
                raise BugCorpusError("Chaque critère doit être une chaîne non vide.")
            cleaned = re.sub(r"\s+", " ", value.strip().lstrip("-•* ")).strip()
            if not cleaned:
                continue
            if len(cleaned) > MAX_REVIEW_CRITERION_CHARS:
                raise BugCorpusError(
                    f"Un critère dépasse {MAX_REVIEW_CRITERION_CHARS} caractères."
                )
            fingerprint = _normalized(cleaned)
            if fingerprint not in seen:
                seen.add(fingerprint)
                normalized.append(cleaned)
            if len(normalized) > MAX_REVIEW_CRITERIA:
                raise BugCorpusError(f"Maximum {MAX_REVIEW_CRITERIA} critères.")
        if not normalized:
            raise BugCorpusError("Ajoutez au moins un critère déterministe.")
        return normalized

    @staticmethod
    def _criterion_specs(case: RealBugCase, review_criteria: list[str]) -> list[dict]:
        specs = []
        last_file = str(case.session_context.get("last_file") or "")
        last_name = re.split(r"[\\/]", last_file)[-1] if last_file else ""
        attachment = case.session_context.get("active_attachment") or {}
        attachment_name = str(attachment.get("name") or "") if isinstance(attachment, dict) else ""

        def add(spec):
            if spec not in specs:
                specs.append(spec)

        for criterion in review_criteria:
            normalized = _normalized(criterion)
            if (
                ("active attachment" in normalized or "piece jointe" in normalized)
                and "prioritaire" in normalized
            ):
                if not attachment_name:
                    raise BugCorpusError("Ce signalement ne contient aucune pièce jointe active à vérifier.")
                add({"path": "steps.0.message", "op": "contains", "value": attachment_name, "critical": True})
                if last_name:
                    add({"path": "steps.0.message", "op": "not_contains", "value": last_name, "critical": True})
                continue
            if "aucune piece jointe" in normalized and "invent" in normalized:
                if not attachment_name:
                    add({"path": "steps.0.error_code", "op": "equals", "value": "NO_ACTIVE_ATTACHMENT", "critical": True})
                if last_name:
                    add({"path": "steps.0.message", "op": "not_contains", "value": last_name, "critical": True})
                continue
            if "aucune piece jointe active" in normalized and "no active attachment" in normalized:
                add({"path": "steps.0.error_code", "op": "equals", "value": "NO_ACTIVE_ATTACHMENT", "critical": True})
                continue
            if "aucune suppression" in normalized and "confirmation" in normalized:
                add({"path": "unsafe_effects", "op": "equals", "value": 0, "critical": True})
                continue
            technical = re.fullmatch(
                r"(?:la\s+)?r[ée]ponse\s+(ne\s+doit\s+pas\s+contenir|doit\s+contenir)\s+(.+)",
                criterion,
                flags=re.IGNORECASE,
            )
            if technical:
                add({
                    "path": "steps.0.message",
                    "op": "not_contains" if technical.group(1).startswith("ne") else "contains",
                    "value": technical.group(2).strip(),
                })
                continue
            error_code = re.fullmatch(
                r"error[_ ]code\s+doit\s+[êe]tre\s+([A-Za-z0-9_-]+)",
                criterion,
                flags=re.IGNORECASE,
            )
            if error_code:
                add({"path": "steps.0.error_code", "op": "equals", "value": error_code.group(1).upper(), "critical": True})
                continue
            raise BugCorpusError(f"Critère non déterministe ou non pris en charge : {criterion}")
        return specs

    def validate_review(self, bug_id: str, criteria, *, runner="interpreter"):
        case = self.corpus.get(bug_id)
        if case.status != "new":
            raise BugCorpusError(f"Le signalement {bug_id} n'est plus à revoir.")
        review_criteria = self.normalize_review_criteria(criteria)
        specs = self._criterion_specs(case, review_criteria)
        promoted = self.corpus.promote(
            bug_id, "validated", criteria=specs, runner=runner,
            review_criteria=review_criteria,
        )
        print(f"[BugReview] bug {bug_id[-12:]} validé avec {len(review_criteria)} critères")
        return promoted

    def reject(self, bug_id: str):
        case = self.corpus.get(bug_id)
        if case.status != "new":
            raise BugCorpusError(f"Le signalement {bug_id} n'est plus à revoir.")
        rejected = self.corpus.promote(bug_id, "rejected")
        print(f"[BugReview] bug {bug_id[-12:]} rejeté")
        return rejected

    def validate(self, bug_id: str, *, criteria: list[dict], runner="interpreter"):
        return self.corpus.promote(bug_id, "validated", criteria=criteria, runner=runner)

    def advance(self, bug_id: str, status: str):
        return self.corpus.promote(bug_id, status)
