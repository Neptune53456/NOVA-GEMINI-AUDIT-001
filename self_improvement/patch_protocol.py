"""Contrat canonique, validation et réparation sûre des propositions de patch."""

from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any


class PatchErrorCode(str, Enum):
    SCHEMA_INVALID = "SCHEMA_INVALID"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    INVALID_OPERATION_TYPE = "INVALID_OPERATION_TYPE"
    INVALID_TARGET = "INVALID_TARGET"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    UNSAFE_PATH = "UNSAFE_PATH"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    STALE_CONTENT = "STALE_CONTENT"
    UNREPAIRABLE_RESPONSE = "UNREPAIRABLE_RESPONSE"


class PatchContractError(ValueError):
    """Erreur structurée sans contenu sensible du fichier."""

    def __init__(
        self,
        code: PatchErrorCode,
        message: str,
        *,
        operation_index: int | None = None,
        field_name: str | None = None,
        retryable: bool = True,
    ) -> None:
        self.code = code
        self.message = message
        self.operation_index = operation_index
        self.field_name = field_name
        self.retryable = retryable
        super().__init__(self.safe_message)

    @property
    def safe_message(self) -> str:
        location = f" operation={self.operation_index}" if self.operation_index is not None else ""
        field = f" field={self.field_name}" if self.field_name else ""
        return f"patch_contract:{self.code.value}:{location}{field} {self.message}".strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "operation_index": self.operation_index,
            "field": self.field_name,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class PatchOperation:
    operation_type: str
    file_path: str
    new_content: str
    target_symbol: str | None = None
    anchor: str | None = None
    old_content: str | None = None
    expected_hash: str | None = None

    def to_legacy_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"action": self.operation_type, "new_text": self.new_content}
        if self.target_symbol is not None:
            data["symbol"] = self.target_symbol
        if self.anchor is not None:
            data["anchor"] = self.anchor
        if self.old_content is not None:
            data["old_text"] = self.old_content
        return data


@dataclass(frozen=True)
class PatchProposal:
    version: str
    files: tuple[str, ...]
    operations: tuple[PatchOperation, ...]
    rationale: str = ""
    expected_behavior: str = ""
    repairs: tuple[str, ...] = ()


@dataclass
class PatchProtocolMetrics:
    counters: Counter = field(default_factory=Counter)
    rejection_reasons: Counter = field(default_factory=Counter)

    def record(self, event: str, reason: str | None = None) -> None:
        self.counters[event] += 1
        if reason:
            self.rejection_reasons[reason] += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "invalid_responses": self.counters["invalid_responses"],
            "deterministic_repairs": self.counters["deterministic_repairs"],
            "format_retries": self.counters["format_retries"],
            "successful_retries": self.counters["successful_retries"],
            "rejected_responses": self.counters["rejected_responses"],
            "rejection_reasons": dict(self.rejection_reasons),
        }


PATCH_PROTOCOL_METRICS = PatchProtocolMetrics()

_ALLOWED_TYPES = {
    "replace", "append", "replace_symbol_block", "insert_after_anchor",
    "insert_before_anchor", "create_file", "full_file_replace",
}
_FIELD_ALIASES = {
    "action": "operation_type", "operation": "operation_type", "type": "operation_type",
    "path": "file_path", "file": "file_path",
    "symbol": "target_symbol", "target": "target_symbol",
    "target_anchor": "anchor", "new_text": "new_content", "content": "new_content",
    "old_text": "old_content", "precondition": "old_content", "hash": "expected_hash",
}
_KNOWN_FIELDS = {
    "operation_type", "file_path", "target_symbol", "anchor", "old_content",
    "new_content", "expected_hash",
}


def _error(code: PatchErrorCode, message: str, index: int | None = None, field: str | None = None, *, retryable: bool = True):
    PATCH_PROTOCOL_METRICS.record("invalid_responses", code.value)
    raise PatchContractError(code, message, operation_index=index, field_name=field, retryable=retryable)


def _safe_relative_path(raw: str) -> str:
    normalized = str(raw).replace("\\", "/").strip()
    candidate = PurePosixPath(normalized)
    if not normalized or candidate.is_absolute() or ".." in candidate.parts or "\x00" in normalized:
        _error(PatchErrorCode.UNSAFE_PATH, "chemin absolu ou traversée interdite", retryable=False)
    return candidate.as_posix()


def _python_symbols(content: str) -> list[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    return [
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


def _normalize_operation(raw: Any, index: int, default_path: str, source_content: str) -> tuple[PatchOperation, list[str]]:
    if not isinstance(raw, dict):
        _error(PatchErrorCode.SCHEMA_INVALID, "l'opération doit être un objet", index)
    normalized: dict[str, Any] = {}
    repairs: list[str] = []
    for key, value in raw.items():
        canonical = _FIELD_ALIASES.get(str(key), str(key))
        if canonical not in _KNOWN_FIELDS:
            _error(PatchErrorCode.UNKNOWN_FIELD, f"champ inconnu: {key}", index, str(key))
        if canonical in normalized and normalized[canonical] != value:
            _error(PatchErrorCode.SCHEMA_INVALID, f"alias contradictoire pour {canonical}", index, canonical)
        normalized[canonical] = value
        if canonical != key:
            repairs.append(f"operation {index}: {key}->{canonical}")

    operation_type = normalized.get("operation_type")
    if not isinstance(operation_type, str) or operation_type not in _ALLOWED_TYPES:
        _error(PatchErrorCode.INVALID_OPERATION_TYPE, "type d'opération absent ou inconnu", index, "operation_type")
    raw_path = normalized.get("file_path", default_path)
    if not isinstance(raw_path, str):
        _error(PatchErrorCode.MISSING_REQUIRED_FIELD, "file_path doit être une chaîne", index, "file_path")
    expected_path = str(default_path).replace("\\", "/")
    if raw_path == default_path:
        file_path = expected_path
    else:
        file_path = _safe_relative_path(raw_path)
        if Path(default_path).is_absolute():
            expected_parts = PurePosixPath(expected_path).parts
            supplied_parts = PurePosixPath(file_path).parts
            same_suffix = (
                len(supplied_parts) <= len(expected_parts)
                and tuple(part.casefold() for part in expected_parts[-len(supplied_parts):])
                == tuple(part.casefold() for part in supplied_parts)
            )
            # Some local models emit a harmless synthetic prefix such as
            # ``path/to/app_controller.py`` even though this protocol has one
            # already-authorized target.  When the basename is identical we
            # can deterministically bind that reference to the sole target;
            # the model never gets to choose or resolve the synthetic path.
            same_basename = (
                bool(supplied_parts)
                and supplied_parts[-1].casefold() == expected_parts[-1].casefold()
            )
            if same_suffix or same_basename:
                file_path = expected_path
                repairs.append(f"operation {index}: relative target canonicalized")
            else:
                _error(PatchErrorCode.OUT_OF_SCOPE, "un chemin explicite ne correspond pas à la cible", index, "file_path", retryable=False)
        else:
            expected_path = _safe_relative_path(default_path)
    if file_path != expected_path:
        _error(PatchErrorCode.OUT_OF_SCOPE, "l'opération cible un autre fichier", index, "file_path", retryable=False)

    new_content = normalized.get("new_content")
    if not isinstance(new_content, str):
        _error(PatchErrorCode.MISSING_REQUIRED_FIELD, "contenu candidat requis", index, "new_content")

    target_symbol = normalized.get("target_symbol")
    anchor = normalized.get("anchor")
    old_content = normalized.get("old_content")
    expected_hash = normalized.get("expected_hash")
    if operation_type == "replace" and not isinstance(old_content, str):
        if isinstance(target_symbol, str) and target_symbol.strip():
            operation_type = "replace_symbol_block"
            repairs.append(f"operation {index}: legacy replace converted to replace_symbol_block")
        elif isinstance(anchor, str) and anchor:
            operation_type = "insert_after_anchor"
            repairs.append(f"operation {index}: legacy replace converted to insert_after_anchor")
        else:
            candidates = _python_symbols(new_content)
            existing = _python_symbols(source_content)
            matches = [name for name in candidates if existing.count(name) == 1]
            if len(matches) == 1:
                operation_type = "replace_symbol_block"
                target_symbol = matches[0]
                repairs.append(f"operation {index}: legacy replace target inferred")
    if expected_hash is not None:
        current_hash = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
        if not isinstance(expected_hash, str) or expected_hash != current_hash:
            _error(PatchErrorCode.STALE_CONTENT, "la précondition de contenu ne correspond plus", index, "expected_hash", retryable=False)

    if operation_type == "replace_symbol_block":
        if not isinstance(target_symbol, str) or not target_symbol.strip():
            candidates = _python_symbols(new_content)
            existing = _python_symbols(source_content)
            matches = [name for name in candidates if existing.count(name) == 1]
            if len(matches) == 1:
                target_symbol = matches[0]
                repairs.append(f"operation {index}: target_symbol inferred")
            elif len(matches) > 1:
                _error(PatchErrorCode.AMBIGUOUS_TARGET, "plusieurs symboles cibles sont possibles", index, "target_symbol")
            else:
                _error(PatchErrorCode.MISSING_REQUIRED_FIELD, "cible symbolique requise et non déductible", index, "target_symbol")
        count = _python_symbols(source_content).count(target_symbol.strip())
        if count == 0:
            _error(PatchErrorCode.INVALID_TARGET, "symbole cible absent", index, "target_symbol")
        if count > 1:
            _error(PatchErrorCode.AMBIGUOUS_TARGET, "symbole cible non unique", index, "target_symbol")
    elif operation_type in {"insert_after_anchor", "insert_before_anchor"}:
        if not isinstance(anchor, str) or not anchor:
            _error(PatchErrorCode.MISSING_REQUIRED_FIELD, "ancre requise", index, "anchor")
        occurrences = source_content.count(anchor)
        if occurrences == 0:
            _error(PatchErrorCode.INVALID_TARGET, "ancre absente", index, "anchor")
        if occurrences > 1:
            _error(PatchErrorCode.AMBIGUOUS_TARGET, "ancre non unique", index, "anchor")
    elif operation_type == "replace":
        if not isinstance(old_content, str) or not old_content:
            _error(PatchErrorCode.MISSING_REQUIRED_FIELD, "précondition old_content requise", index, "old_content")
        occurrences = source_content.count(old_content)
        if occurrences == 0:
            _error(PatchErrorCode.STALE_CONTENT, "old_content absent de l'état courant", index, "old_content", retryable=False)
        if occurrences > 1:
            _error(PatchErrorCode.AMBIGUOUS_TARGET, "old_content non unique", index, "old_content")
    elif operation_type == "append" and old_content is None:
        old_content = ""
        repairs.append(f"operation {index}: optional old_content defaulted")
    # create_file/full_file_replace ne demandent ni symbole ni ancre.

    return PatchOperation(
        operation_type=operation_type,
        file_path=file_path,
        new_content=new_content,
        target_symbol=target_symbol.strip() if isinstance(target_symbol, str) else None,
        anchor=anchor,
        old_content=old_content,
        expected_hash=expected_hash,
    ), repairs


def parse_patch_proposal(payload: Any, *, source_content: str, default_path: str) -> PatchProposal:
    """Normalise les anciens formats puis produit un contrat discriminé validé."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            _error(PatchErrorCode.SCHEMA_INVALID, f"JSON invalide ligne {exc.lineno} colonne {exc.colno}")
    repairs: list[str] = []
    if isinstance(payload, list):
        payload = {"operations": payload}
        repairs.append("legacy operation list wrapped")
    if not isinstance(payload, dict):
        _error(PatchErrorCode.SCHEMA_INVALID, "la proposition doit être un objet")
    operations = payload.get("operations")
    if isinstance(operations, dict):
        operations = [operations]
        repairs.append("single operation wrapped")
    if not isinstance(operations, list) or not operations:
        _error(PatchErrorCode.MISSING_REQUIRED_FIELD, "liste operations non vide requise", field="operations")
    normalized: list[PatchOperation] = []
    for index, raw in enumerate(operations, 1):
        operation, operation_repairs = _normalize_operation(raw, index, default_path, source_content)
        normalized.append(operation)
        repairs.extend(operation_repairs)
    if repairs:
        PATCH_PROTOCOL_METRICS.record("deterministic_repairs")
    files = tuple(dict.fromkeys(operation.file_path for operation in normalized))
    return PatchProposal(
        version=str(payload.get("version", "1")),
        files=files,
        operations=tuple(normalized),
        rationale=str(payload.get("rationale", payload.get("summary", "")) or ""),
        expected_behavior=str(payload.get("expected_behavior", "") or ""),
        repairs=tuple(repairs),
    )
