"""Token/context budgeting utilities for autonomous model calls.

The goal is conservative preflight sizing, not tokenizer-perfect accounting.  A
cheap deterministic estimate is preferable here because it can run before any
remote provider is contacted and works even when provider SDKs are absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Mapping, Any


DEFAULT_CHARS_PER_TOKEN = 3.8


def estimate_text_tokens(text: str, *, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """Return a conservative token estimate for mixed code/natural-language text."""
    raw = str(text or "")
    if not raw:
        return 0
    ratio = max(float(chars_per_token), 1.0)
    # Add a small fixed margin so tiny prompts are not underestimated.
    return max(1, int(len(raw) / ratio) + 8)


def estimate_message_tokens(messages: Iterable[Mapping[str, Any]]) -> int:
    """Estimate chat tokens including a small per-message framing overhead."""
    total = 0
    for message in messages:
        total += 6
        total += estimate_text_tokens(str(message.get("role", "")))
        total += estimate_text_tokens(str(message.get("content", "")))
    return total + 8


def trim_text_to_token_budget(text: str, max_tokens: int) -> str:
    """Deterministically trim text while preserving both its head and tail.

    Repo contexts put inventory/facts near the head and related tests or recent
    additions near the tail; preserving both is materially better than a simple
    prefix cut.
    """
    max_tokens = max(64, int(max_tokens))
    if estimate_text_tokens(text) <= max_tokens:
        return text
    max_chars = max(256, int(max_tokens * DEFAULT_CHARS_PER_TOKEN) - 64)
    marker = "\n\n... [context compacted to fit model token budget] ...\n\n"
    usable = max(128, max_chars - len(marker))
    head = int(usable * 0.72)
    tail = usable - head
    return text[:head] + marker + text[-tail:]


@dataclass(frozen=True)
class PlannerBudget:
    max_input_tokens: int = 5600
    reserve_output_tokens: int = 1200

    @classmethod
    def from_environment(cls) -> "PlannerBudget":
        def read_int(name: str, default: int) -> int:
            try:
                return max(256, int(os.environ.get(name, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            max_input_tokens=read_int("PLANNER_MAX_INPUT_TOKENS", 5600),
            reserve_output_tokens=read_int("PLANNER_RESERVE_OUTPUT_TOKENS", 1200),
        )


@dataclass(frozen=True)
class ContextFitResult:
    messages: list[dict[str, Any]]
    estimated_input_tokens: int
    available_input_tokens: int
    compacted: bool
    strategies: list[str]


def fit_messages_to_context(
    messages: Iterable[Mapping[str, Any]],
    *,
    context_length: int | None,
    max_input_tokens: int | None = None,
    reserve_output_tokens: int = 1024,
    safety_margin: float = 0.12,
) -> ContextFitResult:
    """Retire l'ancien historique puis compacte sans perdre systeme/dernier tour."""
    normalized = [dict(item) for item in messages]
    hard_limit = max_input_tokens or context_length or 32_000
    if context_length:
        hard_limit = min(hard_limit, context_length - max(0, int(reserve_output_tokens)))
    available = max(256, int(hard_limit * (1.0 - max(0.0, min(float(safety_margin), 0.4)))))
    estimated = estimate_message_tokens(normalized)
    if estimated <= available:
        return ContextFitResult(normalized, estimated, available, False, [])

    strategies: list[str] = []
    if len(normalized) > 2:
        system = [item for item in normalized if str(item.get("role", "")).casefold() == "system"][:1]
        normalized = system + normalized[max(0, len(normalized) - 4):]
        strategies.append("drop_old_history")
    if estimate_message_tokens(normalized) > available:
        content_budget = max(64, available - 12 * max(1, len(normalized)))
        per_message = max(64, content_budget // max(1, len(normalized)))
        for item in normalized:
            item["content"] = trim_text_to_token_budget(str(item.get("content", "")), per_message)
        strategies.append("compact_messages")
    return ContextFitResult(normalized, estimate_message_tokens(normalized), available, True, strategies)
