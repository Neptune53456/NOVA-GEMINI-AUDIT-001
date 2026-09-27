"""Sanitized evidence for deterministic failures before public tests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import re


@dataclass(frozen=True)
class PatchFailureEvidence:
    stage: str
    category: str
    file: str | None = None
    symbol: str | None = None
    operation_index: int | None = None
    deterministic_message: str = ""
    repairable: bool = False
    provider: str | None = None
    model: str | None = None
    previous_patch_fingerprint: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def safe_patch_fingerprint(file: str | None, symbol: str | None, operation_family: str,
                           normalized_intent: str) -> str:
    material = "|".join((Path(file or "").name, symbol or "", operation_family,
                         " ".join(normalized_intent.split())[:500]))
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:20]


def classify_patch_failure(message: str, *, file: str | None = None,
                           provider: str | None = None, model: str | None = None,
                           fingerprint: str | None = None) -> PatchFailureEvidence:
    folded = str(message or "").casefold()
    mappings = (
        ("provider_failure", "patch_provider", "PATCH_PROVIDER_FAILURE", False),
        ("all_routes_exhausted", "patch_provider", "PATCH_PROVIDER_FAILURE", False),
        ("invalid_patch_response", "patch_schema", "PATCH_SCHEMA_FAILURE", True),
        ("patch_target_failure", "patch_target", "PATCH_TARGET_FAILURE", True),
        ("patch_contract:schema_invalid", "patch_parse", "PATCH_PARSE_FAILURE", True),
        ("patch_contract:missing_required_field", "patch_schema", "PATCH_SCHEMA_FAILURE", True),
        ("patch_contract:unknown_field", "patch_schema", "PATCH_SCHEMA_FAILURE", True),
        ("out_of_scope", "patch_scope", "PATCH_SCOPE_FAILURE", False),
        ("unsafe_path", "patch_scope", "PATCH_SCOPE_FAILURE", False),
        ("symbol_not_found", "patch_target", "PATCH_TARGET_FAILURE", True),
        ("invalid_target", "patch_target", "PATCH_TARGET_FAILURE", True),
        ("anchor_not_found", "patch_target", "PATCH_TARGET_FAILURE", True),
        ("stale_content", "patch_apply", "PATCH_APPLICATION_FAILURE", True),
        ("write_error", "patch_apply", "PATCH_APPLY_FAILURE", True),
        ("apply_failure", "patch_apply", "PATCH_APPLY_FAILURE", True),
        ("patch_repair_no_progress", "patch_repair", "PATCH_REPAIR_NO_PROGRESS", False),
        ("duplicate_failed_attempt", "patch_repair", "PATCH_REPAIR_NO_PROGRESS", False),
        ("n'existe pas dans le fichier", "patch_apply", "PATCH_APPLICATION_FAILURE", True),
        ("syntax", "patch_syntax", "PATCH_SYNTAX_FAILURE", True),
        ("invalid syntax", "patch_syntax", "PATCH_SYNTAX_FAILURE", True),
        ("import", "patch_import", "PATCH_IMPORT_FAILURE", True),
        ("no_change", "patch_no_effect", "PATCH_NO_EFFECT", False),
        ("aucun fichier modifié", "patch_empty", "PATCH_EMPTY", False),
        ("invalid_patch", "patch_protocol", "PATCH_PROTOCOL_FAILURE", True),
        ("patch_contract", "patch_protocol", "PATCH_PROTOCOL_FAILURE", True),
    )
    stage, category, repairable = "patch_preflight", "PATCH_PROTOCOL_FAILURE", False
    for marker, candidate_stage, candidate_category, candidate_repairable in mappings:
        if marker in folded:
            stage, category, repairable = candidate_stage, candidate_category, candidate_repairable
            break
    operation_match = re.search(r"(?:operation[= ]|patch\s+)(\d+)", str(message), re.IGNORECASE)
    symbol_match = re.search(r"(?:symbol|symbole)[= :'\"]+([A-Za-z_]\w*)", str(message), re.IGNORECASE)
    return PatchFailureEvidence(
        stage=stage, category=category, file=Path(file).name if file else None,
        symbol=symbol_match.group(1) if symbol_match else None,
        operation_index=int(operation_match.group(1)) if operation_match else None,
        deterministic_message=str(message or "")[:500], repairable=repairable,
        provider=provider, model=model, previous_patch_fingerprint=fingerprint,
    )
