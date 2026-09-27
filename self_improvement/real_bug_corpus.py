"""Corpus local, lisible et expurgé des bugs réellement observés dans la GUI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Any

from .models import Scenario


BUG_CORPUS_PATH = Path(__file__).with_name("bugs") / "real_bugs.json"
BUG_STATUSES = {"new", "validated", "integrated", "resolved", "rejected"}
MAX_TEXT_CHARS = 8_000
SECRET_KEYS = {
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "cookie", "cookies", "access_token", "refresh_token",
    "client_secret",
}
STATUS_TRANSITIONS = {
    "new": {"validated", "rejected"},
    "validated": {"integrated"},
    "integrated": {"resolved"},
    "resolved": set(),
    "rejected": set(),
}


class BugCorpusError(ValueError):
    pass


def _normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(character for character in value if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", value).strip()


def _redact_string(value: str) -> str:
    value = value[:MAX_TEXT_CHARS]
    value = re.sub(
        r"(?i)\b(authorization\s*:\s*(?:bearer\s+)?)[^\s,;]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)\b(password|passwd|token|api[_ -]?key|secret|cookie)\s*[:=]\s*([^\s,;]+)",
        lambda match: f"{match.group(1)}=[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
    home = str(Path.home())
    if home:
        value = re.sub(re.escape(home), "<USER_HOME>", value, flags=re.IGNORECASE)
    value = re.sub(
        r"(?i)\b[A-Z]:[\\/]Users[\\/][^\\/\s]+",
        "<USER_HOME>",
        value,
    )
    return value


def sanitize_for_storage(value: Any, *, key="") -> Any:
    """Expurge les secrets nommés et pseudonymise le profil utilisateur."""
    normalized_key = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    if normalized_key in SECRET_KEYS or re.search(
        r"(?:^|_)(?:password|passwd|secret|token|api_?key|authorization|cookies?)(?:_|$)",
        normalized_key,
    ):
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, list):
        return [sanitize_for_storage(item) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_for_storage(item_value, key=str(item_key))
            for item_key, item_value in list(value.items())[:100]
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_string(str(value))


@dataclass(frozen=True)
class RealBugCase:
    id: str
    created_at: str
    status: str
    category: str
    conversation_history: list[dict[str, str]]
    user_message: str
    incorrect_response: str
    expected_behavior: dict[str, Any]
    session_context: dict[str, Any]
    has_attachment: bool
    family_id: str
    variants: list[str]
    source: str = "gui"
    tags: list[str] | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,120}", self.id):
            raise BugCorpusError("Identifiant de bug invalide.")
        if self.status not in BUG_STATUSES:
            raise BugCorpusError(f"Statut de bug invalide : {self.status}")
        if not isinstance(self.category, str) or not isinstance(self.user_message, str) or not self.category.strip() or not self.user_message.strip():
            raise BugCorpusError("Catégorie et message utilisateur sont obligatoires.")
        if not isinstance(self.expected_behavior, dict) or not isinstance(self.expected_behavior.get("criteria"), list):
            raise BugCorpusError("Le comportement attendu doit définir des critères déterministes.")
        if self.status not in {"new", "rejected"} and not self.expected_behavior["criteria"]:
            raise BugCorpusError("Un bug promu doit définir au moins un critère déterministe.")
        if not isinstance(self.session_context, dict) or not isinstance(self.has_attachment, bool):
            raise BugCorpusError("Contexte de session ou présence de pièce jointe invalide.")
        if not isinstance(self.family_id, str) or not self.family_id.strip():
            raise BugCorpusError("La famille du bug est obligatoire.")
        if not isinstance(self.variants, list) or not all(isinstance(item, str) and item.strip() for item in self.variants):
            raise BugCorpusError("Les variantes du bug sont invalides.")
        if not isinstance(self.conversation_history, list):
            raise BugCorpusError("Historique de conversation invalide.")
        for exchange in self.conversation_history:
            if not isinstance(exchange, dict) or set(exchange) != {"role", "content"} or exchange["role"] not in {"user", "assistant"} or not isinstance(exchange["content"], str):
                raise BugCorpusError("Historique de conversation invalide.")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RealBugCase":
        required = {
            "id", "created_at", "status", "category", "conversation_history",
            "user_message", "incorrect_response", "expected_behavior",
            "session_context", "has_attachment", "family_id", "variants",
        }
        allowed = required | {"source", "tags"}
        missing = required - set(payload)
        if missing:
            raise BugCorpusError(f"Bug incomplet : {sorted(missing)}")
        unknown = set(payload) - allowed
        if unknown:
            raise BugCorpusError(f"Champs de bug inconnus : {sorted(unknown)}")
        values = {key: payload[key] for key in required}
        values["source"] = payload.get("source", "gui")
        values["tags"] = list(payload.get("tags", []))
        return cls(**values)

    @property
    def fingerprint(self) -> str:
        stable = {
            "family": self.family_id,
            "message": _normalized_text(self.user_message),
            "context": sanitize_for_storage(self.session_context),
            "expected": sanitize_for_storage(self.expected_behavior),
        }
        encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def sanitized(self) -> "RealBugCase":
        payload = sanitize_for_storage(asdict(self))
        return RealBugCase.from_dict(payload)


class RealBugCorpus:
    def __init__(self, path: Path | str = BUG_CORPUS_PATH):
        self.path = Path(path)

    def load(self, *, statuses=None) -> list[RealBugCase]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BugCorpusError(f"Corpus de bugs illisible : {error}") from error
        if payload.get("version") != "1.0" or not isinstance(payload.get("cases"), list):
            raise BugCorpusError("Schéma du corpus de bugs invalide.")
        cases = [RealBugCase.from_dict(item) for item in payload["cases"]]
        fingerprints = [case.fingerprint for case in cases]
        if len(fingerprints) != len(set(fingerprints)):
            raise BugCorpusError("Le corpus contient des bugs dupliqués.")
        selected = set(statuses) if statuses is not None else None
        return [case for case in cases if selected is None or case.status in selected]

    def _write(self, cases: list[RealBugCase]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": "1.0",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "cases": [asdict(case.sanitized()) for case in cases],
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False, newline="\n"
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(self.path)

    def add(self, case: RealBugCase | dict[str, Any]) -> RealBugCase:
        candidate = case if isinstance(case, RealBugCase) else RealBugCase.from_dict(case)
        candidate = candidate.sanitized()
        cases = self.load()
        if any(existing.fingerprint == candidate.fingerprint for existing in cases):
            raise BugCorpusError("Ce bug réel est déjà enregistré.")
        if any(existing.id == candidate.id for existing in cases):
            raise BugCorpusError("Cet identifiant de bug existe déjà.")
        self._write([*cases, candidate])
        return candidate

    def update_status(self, bug_id: str, status: str):
        return self.promote(bug_id, status)

    def get(self, bug_id: str) -> RealBugCase:
        for case in self.load():
            if case.id == bug_id:
                return case
        raise BugCorpusError(f"Bug inconnu : {bug_id}")

    def list_new(self) -> list[RealBugCase]:
        return self.load(statuses={"new"})

    def promote(
        self, bug_id: str, status: str, *, criteria=None,
        runner="interpreter", review_criteria=None,
    ) -> RealBugCase:
        """Effectue une transition de revue explicite, jamais une promotion implicite."""
        if status not in BUG_STATUSES:
            raise BugCorpusError(f"Statut de bug invalide : {status}")
        cases = self.load()
        current = next((case for case in cases if case.id == bug_id), None)
        if current is None:
            raise BugCorpusError(f"Bug inconnu : {bug_id}")
        if status not in STATUS_TRANSITIONS[current.status]:
            raise BugCorpusError(f"Transition interdite : {current.status} -> {status}")
        expected = dict(current.expected_behavior)
        if status == "validated":
            if not isinstance(criteria, list) or not criteria:
                raise BugCorpusError("La validation manuelle exige des critères déterministes.")
            expected["criteria"] = [dict(item) for item in criteria]
            expected["runner"] = runner
            if review_criteria is not None:
                expected["review_criteria"] = list(review_criteria)
        promoted = replace(current, status=status, expected_behavior=expected)
        updated = [promoted if case.id == bug_id else case for case in cases]
        self._write(updated)
        return promoted

    @staticmethod
    def to_scenario(case: RealBugCase, *, split="train") -> Scenario:
        if case.status not in {"validated", "integrated", "resolved"}:
            raise BugCorpusError("Seuls les bugs validés peuvent devenir des scénarios.")
        # L'historique documente la reproduction GUI. Le contexte de session est
        # l'état initial exécutable : rejouer seulement les tours utilisateur
        # sans leurs réponses assistant fausserait la conversation.
        messages = [case.user_message]
        tags = list(dict.fromkeys([
            *(case.tags or []), "real-bug", f"family:{case.family_id}", case.source,
        ]))
        return Scenario(
            id=f"realbug-{case.id}", category=case.category, split=split,
            initial_context=dict(case.session_context), messages=messages,
            simulated_state={}, expectations=dict(case.expected_behavior),
            success_criteria=[dict(item) for item in case.expected_behavior["criteria"]],
            weight=1.0, tags=tags,
            runner=str(case.expected_behavior.get("runner", "interpreter")),
        )
