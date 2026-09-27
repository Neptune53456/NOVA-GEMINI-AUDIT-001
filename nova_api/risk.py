"""Context-aware, deterministic risk assessment for Nova actions.

The engine complements static Capability metadata. It does not grant permissions;
it only raises risk and can require confirmation when deterministic context warrants it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .memory_store import contains_obvious_secret

RiskLevel = Literal["low", "medium", "high", "critical"]
_LEVEL = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_SECRET_VALUE = re.compile(
    r"(?i)(?:password|passwd|mot\s+de\s+passe|api[_ -]?key|access[_ -]?token|auth[_ -]?token|secret)\s*[:=]|"
    r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b"
)
_SENSITIVE_PATH = re.compile(r"(?i)(?:^|/)(?:\.env|credentials?|secrets?|id_rsa|id_ed25519)(?:\.|$|/)")
_SENSITIVE_APP = re.compile(r"(?i)(?:credential|security|bitwarden|1password|keepass|regedit|registry|installer|setup|powershell|pwsh|cmd(?:\.exe)?|terminal)")
_HIGH_IMPACT_TARGET = re.compile(r"(?i)(?:delete|remove|uninstall|format|reset|purchase|buy|pay|send|transfer|publish|deploy|permission|administrator|admin|sign[ -]?in|log[ -]?in|password|credential|2fa|two[ -]?factor)")
_SENSITIVE_WINDOW = re.compile(r"(?i)(?:bank|payment|checkout|wallet|security|credential|password|administrator|permissions?|billing|purchase|transfer)")
_CRITICAL_TARGET = re.compile(r"(?i)(?:confirm\s+(?:purchase|payment|transfer)|place\s+order|buy\s+now|send\s+money|wire\s+transfer|format\s+disk|disable\s+(?:security|firewall|antivirus)|factory\s+reset)")


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    requires_confirmation: bool
    reasons: tuple[str, ...]
    reversible: bool

    def public(self) -> dict[str, object]:
        return {
            "level": self.level,
            "requires_confirmation": self.requires_confirmation,
            "reasons": list(self.reasons),
            "reversible": self.reversible,
        }


class RiskEngine:
    """Raise risk from effect/target context without weakening static policy."""

    def assess(self, capability: Any, arguments: dict[str, Any] | None = None, *,
               context: Mapping[str, Any] | None = None) -> RiskAssessment:
        args = arguments or {}
        ctx = dict(context or {})
        level: RiskLevel = capability.risk_level
        reasons = [f"base:{capability.risk_level}"]
        requires = bool(capability.requires_confirmation)

        def raise_to(candidate: RiskLevel, reason: str) -> None:
            nonlocal level
            if _LEVEL[candidate] > _LEVEL[level]:
                level = candidate
            reasons.append(reason)

        cid = str(capability.id)
        category = str(capability.category)

        if cid.startswith("computer.ui.") and cid not in {"computer.ui.inspect", "computer.ui.elements", "computer.ui.focus"}:
            raise_to("medium", "desktop_state_mutation")
        if cid == "computer.ui.set_value":
            value = str(args.get("value", ""))
            if contains_obvious_secret(value) or _SECRET_VALUE.search(value):
                raise_to("high", "secret_like_input")
                requires = True
        if cid == "filesystem.write":
            path = str(args.get("path", "")).replace("\\", "/")
            raise_to("medium", "filesystem_mutation")
            if _SENSITIVE_PATH.search(path):
                raise_to("high", "sensitive_target")
                requires = True
        if category == "computer" and not capability.reversible and not cid.startswith("computer.visual."):
            raise_to("high", "irreversible_desktop_effect")
            requires = True

        application = str(ctx.get("application") or "")
        window_title = str(ctx.get("window_title") or "")
        target_name = str(ctx.get("target_name") or "")
        target_role = str(ctx.get("target_role") or "")
        if ctx.get("protected") is True or ctx.get("password") is True:
            raise_to("high", "protected_target")
            requires = True
        if application and _SENSITIVE_APP.search(application):
            raise_to("high", "sensitive_application")
            requires = True
        if window_title and _SENSITIVE_WINDOW.search(window_title):
            raise_to("high", "sensitive_window_context")
            requires = True
        if target_name and _HIGH_IMPACT_TARGET.search(target_name):
            raise_to("high", "high_impact_target")
            requires = True
        if target_name and _CRITICAL_TARGET.search(target_name):
            raise_to("critical", "critical_external_effect")
            requires = True
        if ctx.get("external_side_effect") is True and not capability.reversible:
            raise_to("critical", "irreversible_external_side_effect")
            requires = True
        if ctx.get("data_sensitive") is True:
            raise_to("high", "sensitive_data_context")
            requires = True
        if ctx.get("uncertainty_level") in {"high", "critical"} and level in {"high", "critical"}:
            raise_to("critical", "high_risk_under_uncertainty")
            requires = True
        if target_role.casefold() in {"password", "credential"}:
            raise_to("high", "credential_control")
            requires = True
        if ctx.get("ambiguous") is True:
            raise_to("high", "ambiguous_target")
            requires = True
        if level in {"high", "critical"}:
            requires = True

        return RiskAssessment(level, requires, tuple(dict.fromkeys(reasons)), bool(capability.reversible))
