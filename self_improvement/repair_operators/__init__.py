"""Opérateurs déterministes du Self-Repair Engine."""

from .base import (
    OperatorApplicability, RepairCandidate, RepairOperator, RepairProposal,
    TextEdit, UnsafeRepairError,
)
from .operators import (
    ConfirmationRepairOperator,
    ContextPriorityRepairOperator,
    DEFAULT_OPERATORS,
    DocumentRoutingRepairOperator,
    IntentAliasRepairOperator,
    RegexRepairOperator,
    TextNormalizationRepairOperator,
)

__all__ = [
    "OperatorApplicability", "RepairCandidate", "RepairOperator", "RepairProposal",
    "TextEdit", "UnsafeRepairError",
    "TextNormalizationRepairOperator", "RegexRepairOperator",
    "IntentAliasRepairOperator", "ConfirmationRepairOperator",
    "DocumentRoutingRepairOperator", "ContextPriorityRepairOperator", "DEFAULT_OPERATORS",
]
