"""Evidence-driven bounded recovery selection with anti-spin."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RecoveryAction = Literal[
    "retry_same", "reobserve", "reresolve_target", "switch_capability", "fallback_visual",
    "retrieve_experience", "replan", "deliberate", "request_confirmation", "request_user_input",
    "rollback", "fail_safely",
]

_FAILURE_ALIASES = {
    "stale_ui_reference": "STALE_UI_REFERENCE",
    "stale_element_reference": "STALE_UI_REFERENCE",
    "stale_window_ref": "STALE_UI_REFERENCE",
    "stale_window_reference": "STALE_UI_REFERENCE",
    "target_fingerprint_changed": "STATE_CHANGED_EXTERNALLY",
    "ambiguous_ui": "AMBIGUOUS_UI",
    "ambiguous_element_reference": "AMBIGUOUS_UI",
    "ambiguous_perception_target": "AMBIGUOUS_UI",
    "target_not_found": "TARGET_NOT_FOUND",
    "element_not_found": "TARGET_NOT_FOUND",
    "window_not_found": "TARGET_NOT_FOUND",
    "perception_target_not_found": "TARGET_NOT_FOUND",
    "verification_failed": "VERIFICATION_FAILED",
    "goal_verification_failed": "VERIFICATION_FAILED",
    "transient_provider": "TRANSIENT_PROVIDER",
    "transient_server_error": "TRANSIENT_PROVIDER",
    "provider_unavailable": "TRANSIENT_PROVIDER",
    "vision_provider_unavailable": "TRANSIENT_PROVIDER",
    "capability_unavailable": "CAPABILITY_UNAVAILABLE",
    "no_tool_capable_provider": "CAPABILITY_UNAVAILABLE",
    "no_vision_capable_provider": "CAPABILITY_UNAVAILABLE",
    "permission_required": "PERMISSION_REQUIRED",
    "confirmation_required": "PERMISSION_REQUIRED",
    "risk_escalated": "RISK_ESCALATED",
    "state_changed_externally": "STATE_CHANGED_EXTERNALLY",
    "memory_strategy_failed": "MEMORY_STRATEGY_FAILED",
    "plan_invalid": "PLAN_INVALID",
    "action_uncertain": "ACTION_UNCERTAIN",
    "interrupted_uncertain": "ACTION_UNCERTAIN",
    "action_partial": "ACTION_PARTIAL",
    "repeated_action": "REPEATED_ACTION",
}


def normalize_failure_category(value: str) -> str:
    key = str(value or "UNKNOWN").strip().replace("-", "_").casefold()
    return _FAILURE_ALIASES.get(key, key.upper())


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    repeated_strategy_blocked: bool = False


class RecoveryPolicy:
    def decide(self, *, failure_category: str, same_strategy_failures: int = 0,
               reversible: bool = False, uncertainty_level: str = "low") -> RecoveryDecision:
        category = normalize_failure_category(failure_category)
        if same_strategy_failures >= 2 or category == "REPEATED_ACTION":
            return RecoveryDecision(
                "replan" if uncertainty_level != "critical" else "deliberate",
                "anti_spin_repeated_strategy",
                True,
            )
        mapping: dict[str, RecoveryAction] = {
            "STALE_UI_REFERENCE": "reresolve_target",
            "AMBIGUOUS_UI": "fallback_visual",
            "TARGET_NOT_FOUND": "reobserve",
            "VERIFICATION_FAILED": "reobserve",
            "TRANSIENT_PROVIDER": "retry_same",
            "CAPABILITY_UNAVAILABLE": "switch_capability",
            "PERMISSION_REQUIRED": "request_confirmation",
            "RISK_ESCALATED": "request_confirmation",
            "STATE_CHANGED_EXTERNALLY": "reobserve",
            "MEMORY_STRATEGY_FAILED": "retrieve_experience",
            "PLAN_INVALID": "replan",
            "ACTION_UNCERTAIN": "reobserve",
            "ACTION_PARTIAL": "rollback" if reversible else "fail_safely",
        }
        action = mapping.get(category, "replan" if uncertainty_level in {"medium", "high", "critical"} else "fail_safely")
        return RecoveryDecision(action, category.casefold())
