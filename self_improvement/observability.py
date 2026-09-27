"""Observabilite JSONL persistante V7, recursivement sanitisee."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock
from typing import Any

from self_improvement.experiment_memory import sanitize_value


class AgentTelemetry:
    def __init__(self, repo_root: str | Path, path: str | Path | None = None) -> None:
        root = Path(repo_root).resolve()
        self.path = Path(path) if path else root / ".runtime" / "telemetry.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def emit(self, event: str, **data: Any) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": str(event)[:80],
            **sanitize_value(data),
        }
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def model_call(
        self,
        *,
        agent: str,
        provider: str,
        model: str,
        attempt: int,
        latency_ms: int,
        estimated_input_tokens: int,
        result: str,
        campaign_id: str | None = None,
        task_id: str | None = None,
        output_tokens: int | None = None,
        error_kind: str | None = None,
        status_code: int | None = None,
        cooldown: float | None = None,
        fallback_reason: str | None = None,
    ) -> None:
        self.emit(
            "model_call",
            campaign_id=campaign_id, task_id=task_id, agent=agent,
            provider=provider, model=model, attempt=attempt,
            latency_ms=latency_ms, estimated_input_tokens=estimated_input_tokens,
            output_tokens=output_tokens, result=result, error_kind=error_kind,
            status_code=status_code, cooldown=cooldown, fallback_reason=fallback_reason,
        )

    def task_event(self, *, phase: str, campaign_id: str | None = None, task_id: str | None = None, **payload: Any) -> None:
        self.emit("task", campaign_id=campaign_id, task_id=task_id, phase=phase, **payload)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        total = 0
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                    event = str(row.get("event", "unknown"))
                except json.JSONDecodeError:
                    continue
                counts[event] = counts.get(event, 0) + 1
                total += 1
        return {"events": total, "by_type": counts, "path": str(self.path)}
