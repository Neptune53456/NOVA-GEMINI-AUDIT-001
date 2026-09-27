"""Historique local, dédupliqué et expurgé des données holdout."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .reporting import write_json


HISTORY_VERSION = 2
EVALUATION_VERSION = "fast-gate-v2"
ENGINE_SCHEMA_VERSION = "self-repair-history-v2"
LEGACY_EVALUATION_VERSION = "legacy"
STALE_FAST_GATE_REASON = "legacy_targeted_tests_was_actually_full_pytest"
DERIVED_PATCH_INVALID_REASON = "Patch identique déjà rejeté pour cette cause."
FORBIDDEN_KEYS = frozenset({"scenario_id", "messages", "message", "family", "formulation", "holdout"})


def patch_signature(candidate) -> str:
    def canonical(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    edits = [
        {"path": edit.path.replace("\\", "/").casefold(), "old": canonical(edit.old),
         "new": canonical(edit.new), "occurrences": edit.expected_occurrences}
        for edit in candidate.edits
    ]
    raw = json.dumps(edits, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe(value: Any, key: str = "") -> Any:
    if key.casefold() in FORBIDDEN_KEYS or "holdout" in key.casefold():
        return None
    if isinstance(value, dict):
        return {name: sanitized for name, item in value.items() if (sanitized := _safe(item, name)) is not None}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _safe_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    """Expurge un enregistrement, y compris les détails textuels du holdout."""
    sanitized = _safe(dict(attempt))
    if sanitized.get("rejection_stage") == "holdout":
        sanitized["rejection_reason"] = ""
    return sanitized


class RepairHistory:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self.path = self.directory / "attempts.json"

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        attempts = payload.get("attempts", []) if isinstance(payload, dict) else []
        return [self._migrate(item) for item in attempts if isinstance(item, dict)]

    @staticmethod
    def _migrate(item: dict[str, Any]) -> dict[str, Any]:
        migrated = dict(item)
        migrated.setdefault("evaluation_version", LEGACY_EVALUATION_VERSION)
        migrated.setdefault("engine_schema_version", "self-repair-history-v1")
        migrated.setdefault("baseline_commit", None)
        migrated.setdefault("canonical_cause", migrated.get("root_cause_id"))
        migrated.setdefault("created_at", migrated.get("recorded_at"))
        migrated.setdefault("retry_count", 0)
        migrated.setdefault("stale_reason", None)
        return migrated

    @staticmethod
    def _is_known_stale_rejection(item: dict[str, Any]) -> bool:
        return (
            item.get("evaluation_version") == LEGACY_EVALUATION_VERSION
            and item.get("outcome") == "rejected"
            and item.get("rejection_stage") == "targeted_tests"
            and item.get("rejection_reason") == "Suite pytest locale échouée."
        )

    @staticmethod
    def _is_non_substantive_history_item(item: dict[str, Any]) -> bool:
        stage = item.get("rejection_stage")
        reason = item.get("rejection_reason")
        normalized = " ".join(str(reason).split()) if reason is not None else ""

        if stage == "patch_invalid" and normalized == DERIVED_PATCH_INVALID_REASON:
            return True

        if stage == "internal_error" and "working tree principal" in normalized.casefold():
            lowered = normalized.casefold()
            if "doit être propre" in lowered and "cycle réel" in lowered:
                return True

        return False

    @staticmethod
    def _is_derived_history_block(item: dict[str, Any]) -> bool:
        if item.get("rejection_stage") != "patch_invalid":
            return False
        reason = item.get("rejection_reason")
        if not isinstance(reason, str):
            return False
        return " ".join(reason.split()) == DERIVED_PATCH_INVALID_REASON

    @staticmethod
    def _is_composable_insufficient_gain(item: dict[str, Any]) -> bool:
        return (
            item.get("evaluation_version") == EVALUATION_VERSION
            and item.get("outcome") == "rejected"
            and item.get("rejection_stage") == "insufficient_gain"
            and item.get("bundle_composable") is True
        )

    @staticmethod
    def _sort_key(item: dict[str, Any]) -> str:
        return str(item.get("recorded_at") or item.get("created_at") or "")

    def _latest_authoritative_item(self, matching: list[dict[str, Any]]) -> dict[str, Any] | None:
        authoritative = [
            item for item in matching
            if not self._is_known_stale_rejection(item)
            and not self._is_non_substantive_history_item(item)
        ]
        if not authoritative:
            return None
        return max(authoritative, key=self._sort_key)

    def authorize_retry(
        self, root_cause_id: str, signature: str, *, operator: str | None = None,
        canonical_cause: str | None = None, baseline_commit: str | None = None,
        rejection_stage: str | None = None, rejection_reason: str | None = None,
        stale_reason: str | None = None,
    ) -> None:
        matching = [
            item for item in self.load()
            if item.get("root_cause_id") == root_cause_id
            and item.get("patch_signature") == signature
        ]
        if not matching:
            return
        current = next((item for item in matching if item.get("outcome") == "retry_authorized"), None)
        if current is not None:
            return
        existing = self._latest_authoritative_item(matching) or matching[0]
        self.record({
            "root_cause_id": root_cause_id,
            "canonical_cause": canonical_cause or existing.get("canonical_cause"),
            "operator": operator or existing.get("operator"),
            "patch_signature": signature,
            "outcome": "retry_authorized",
            "rejection_stage": rejection_stage or existing.get("rejection_stage"),
            "rejection_reason": rejection_reason or existing.get("rejection_reason"),
            "evaluation_version": EVALUATION_VERSION,
            "baseline_commit": baseline_commit,
            "retry_count": 1,
            "stale_reason": stale_reason or STALE_FAST_GATE_REASON,
        })

    def was_rejected(
        self, root_cause_id: str, signature: str, *, operator: str | None = None,
        canonical_cause: str | None = None, baseline_commit: str | None = None,
    ) -> bool:
        matching = [
            item for item in self.load()
            if item.get("root_cause_id") == root_cause_id
            and item.get("patch_signature") == signature
        ]
        if not matching:
            return False
        if any(item.get("outcome") == "retry_authorized" for item in matching):
            return True
        stale = next((item for item in matching if self._is_known_stale_rejection(item)), None)
        if stale is not None:
            self.record({
                "root_cause_id": root_cause_id,
                "canonical_cause": canonical_cause or stale.get("canonical_cause"),
                "operator": operator or stale.get("operator"),
                "patch_signature": signature,
                "outcome": "retry_authorized",
                "rejection_stage": stale.get("rejection_stage"),
                "rejection_reason": stale.get("rejection_reason"),
                "evaluation_version": EVALUATION_VERSION,
                "baseline_commit": baseline_commit,
                "retry_count": 1,
                "stale_reason": STALE_FAST_GATE_REASON,
            })
            return False
        authoritative = [
            item for item in matching
            if item.get("outcome") == "rejected"
            and not self._is_known_stale_rejection(item)
            and not self._is_non_substantive_history_item(item)
        ]
        if not authoritative:
            return False
        latest = max(authoritative, key=self._sort_key)
        return latest.get("rejection_stage") is not None

    def record(self, attempt: dict[str, Any]) -> None:
        attempts = [_safe_attempt(item) for item in self.load()]
        sanitized = _safe_attempt(attempt)
        now = datetime.now(timezone.utc).isoformat()
        sanitized.setdefault("evaluation_version", EVALUATION_VERSION)
        sanitized.setdefault("engine_schema_version", ENGINE_SCHEMA_VERSION)
        sanitized.setdefault("baseline_commit", None)
        sanitized.setdefault("canonical_cause", sanitized.get("root_cause_id"))
        sanitized.setdefault("retry_count", 0)
        sanitized.setdefault("stale_reason", None)
        sanitized.setdefault("created_at", now)
        sanitized["recorded_at"] = now
        identity = (
            sanitized.get("root_cause_id"), sanitized.get("operator"),
            sanitized.get("patch_signature"), sanitized.get("outcome"),
            sanitized.get("rejection_stage"), sanitized.get("evaluation_version"),
            sanitized.get("bundle_composable"), sanitized.get("rejection_reason"),
        )
        if not any(
            (item.get("root_cause_id"), item.get("operator"), item.get("patch_signature"),
             item.get("outcome"), item.get("rejection_stage"), item.get("evaluation_version"),
             item.get("bundle_composable"), item.get("rejection_reason")) == identity
            for item in attempts
        ):
            attempts.append(sanitized)
        write_json(self.path, {"version": HISTORY_VERSION, "attempts": attempts})

    def operator_statistics(self) -> dict[str, dict[str, Any]]:
        grouped = defaultdict(list)
        for item in self.load():
            grouped[item.get("operator", "unknown")].append(item)
        statistics = {}
        for name, items in grouped.items():
            accepted = [item for item in items if item.get("outcome") == "accepted"]
            stages = Counter(item.get("rejection_stage") for item in items if item.get("rejection_stage"))
            gains = [float(item["train_gain"]) for item in items if isinstance(item.get("train_gain"), (int, float))]
            durations = [float(item["duration_seconds"]) for item in items if isinstance(item.get("duration_seconds"), (int, float))]
            statistics[name] = {
                "attempts": len(items), "accepted": len(accepted), "rejected": len(items) - len(accepted),
                "acceptance_rate": round(len(accepted) / len(items), 4) if items else 0.0,
                "average_gain": round(sum(gains) / len(gains), 4) if gains else "not_available",
                "average_duration": round(sum(durations) / len(durations), 4) if durations else "not_available",
                "common_rejection_stages": dict(stages.most_common()),
                "root_causes_supported": sorted({item.get("root_cause_id") for item in items if item.get("root_cause_id")}),
                "last_success": accepted[-1].get("recorded_at") if accepted else None,
            }
        return statistics

    def priority_multiplier(self, operator: str) -> float:
        stats = self.operator_statistics().get(operator)
        if not stats or stats["attempts"] < 3:
            return 1.0
        # Le passé influence l'ordre, jamais une désactivation définitive.
        return max(0.5, min(1.5, 0.75 + float(stats["acceptance_rate"])))

    def bundle_eligibility(
        self,
        root_cause_id: str,
        signature: str,
    ) -> str:
        matching = [
            item for item in self.load()
            if item.get("root_cause_id") == root_cause_id
            and item.get("patch_signature") == signature
        ]
        if not matching:
            return "new"

        authoritative = [
            item for item in matching
            if item.get("outcome") == "rejected"
            and not self._is_known_stale_rejection(item)
            and not self._is_non_substantive_history_item(item)
        ]
        retry_records = [
            item for item in matching
            if item.get("outcome") == "retry_authorized"
        ]

        if not authoritative:
            return "blocked" if retry_records else "new"

        latest_authoritative = max(authoritative, key=self._sort_key)
        latest_retry = max(retry_records, key=self._sort_key) if retry_records else None
        latest_non_substantive = max(
            (item for item in matching if self._is_non_substantive_history_item(item)),
            key=self._sort_key,
            default=None,
        )

        if (
            latest_retry is not None
            and latest_non_substantive is not None
            and self._sort_key(latest_non_substantive) >= self._sort_key(latest_retry)
            and self._sort_key(latest_authoritative) < self._sort_key(latest_non_substantive)
        ):
            latest_retry = None

        if latest_retry is not None and self._sort_key(latest_retry) >= self._sort_key(latest_authoritative):
            return "blocked"

        if latest_authoritative.get("rejection_stage") == "insufficient_gain":
            if latest_authoritative.get("bundle_composable") is True:
                return "composable"
            if latest_authoritative.get("bundle_composable") is False:
                return "blocked"
            return "revalidate"
        return "blocked"
