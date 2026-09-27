"""Shared deterministic execution primitives for GoalRunner and MissionManager.

Nova 1.1 Phase 2 does not merge the two orchestrators.  This kernel extracts the
restart/idempotency contracts that must behave identically in both runtimes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from .capabilities import CapabilityRegistry
from .durable_state import action_fingerprint

MutationState = Literal["NOT_STARTED", "STARTED_UNCERTAIN", "COMPLETED", "VERIFIED", "ROLLED_BACK"]


def stable_mutation_id(owner_id: str, step_id: str, capability_id: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        [owner_id, step_id, capability_id, action_fingerprint(capability_id, arguments)],
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecoveryDecision:
    state: Literal["completed", "not_applied", "conflict", "none", "unsupported"]
    transaction_id: str | None = None


class ExecutionKernel:
    """Common restart/idempotency logic, deliberately independent of orchestration state."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    @staticmethod
    def mutation_entry(owner_id: str, step_id: str, capability_id: str,
                       arguments: dict[str, Any], state: MutationState, **metadata: Any) -> dict[str, Any]:
        return {
            "mutation_id": stable_mutation_id(owner_id, step_id, capability_id, arguments),
            "step_id": step_id,
            "capability_id": capability_id,
            "effect_fingerprint": action_fingerprint(capability_id, arguments),
            "state": state,
            **{key: value for key, value in metadata.items() if value is not None},
        }

    def reconcile(self, capability_id: str, arguments: dict[str, Any]) -> RecoveryDecision:
        """Re-observe an uncertain mutation without replaying it."""
        if capability_id != "filesystem.write" or self.registry.transactions is None:
            return RecoveryDecision("unsupported")
        expected_hash = hashlib.sha256(str(arguments.get("content", "")).encode("utf-8")).hexdigest()
        state, transaction_id = self.registry.transactions.reconcile_pending(str(arguments.get("path", "")), expected_hash)
        return RecoveryDecision(state, transaction_id)
