"""Authoritative bounded assembly of durable, project and mission context."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .memory_store import MemoryStore, RetrievedMemory, should_retrieve_memory
from .project_brain import ProjectBrain

if TYPE_CHECKING:
    from .missions import MissionManager

MAX_CONTEXT_CHARS = 18_000
MEMORY_BUDGET_CHARS = 5_000
PROJECT_BUDGET_CHARS = 10_000
MISSION_BUDGET_CHARS = 2_000
EXPERIENCE_BUDGET_CHARS = 1_800
MISSION_RESULT_BUDGET_CHARS = 700


@dataclass(frozen=True)
class ContextPackage:
    content: str
    estimated_chars: int
    diagnostics: tuple[dict[str, Any], ...]
    memory_ids: tuple[str, ...]


class ContextBuilder:
    def __init__(self, memory: MemoryStore, *, project_brain: ProjectBrain | None = None,
                 missions: MissionManager | None = None) -> None:
        self.memory, self.project_brain, self.missions = memory, project_brain, missions

    def build(self, request: str, *, conversation_id: str | None = None) -> ContextPackage:
        chunks: list[str] = []
        diagnostics: list[dict[str, Any]] = []
        ids: list[str] = []

        # One retrieval pass feeds both durable memory and experience. This avoids
        # duplicate embedding work/database scans on every planning request.
        retrieve_general = should_retrieve_memory(request) or self.memory.semantic_enabled
        candidates = self.memory.search(request, limit=20, touch=False)

        if retrieve_general:
            used = 0
            for result in candidates:
                if result.item.memory_type in {"OUTCOME", "ERROR_LESSON"}:
                    continue
                line = self._memory_line(result)
                if used + len(line) > MEMORY_BUDGET_CHARS:
                    continue
                chunks.append(line)
                used += len(line)
                ids.append(result.item.memory_id)
                diagnostics.append({
                    "source": "memory", "reference": result.item.memory_id,
                    "reasons": list(result.reasons), "score": round(result.score, 2),
                })
                if used >= MEMORY_BUDGET_CHARS:
                    break

        # Outcome/error lessons are useful even when the user did not explicitly
        # ask for memory. They remain a small, explicitly untrusted advice channel.
        experience_used = 0
        for result in candidates:
            if result.item.memory_type not in {"OUTCOME", "ERROR_LESSON"}:
                continue
            if result.item.memory_id in ids:
                continue
            line = "[EXPERIENCE; untrusted advice] " + self._memory_line(result)
            if experience_used + len(line) > EXPERIENCE_BUDGET_CHARS:
                continue
            chunks.append(line)
            experience_used += len(line)
            ids.append(result.item.memory_id)
            diagnostics.append({
                "source": "experience", "reference": result.item.memory_id,
                "reasons": list(result.reasons), "score": round(result.score, 2),
            })
            if experience_used >= EXPERIENCE_BUDGET_CHARS:
                break

        if ids:
            self.memory.touch(ids)

        # Active mission state outranks broad repository context. Reserving it first
        # keeps long-running work from disappearing when ProjectBrain has a large payload.
        if self.missions is not None:
            continuity_requested = should_retrieve_memory(request)
            active_states = {"pending", "running", "awaiting_confirmation", "paused"}
            if conversation_id:
                relevant = self.missions.store.list(
                    conversation_id=conversation_id, states=active_states, limit=3
                )
                if continuity_requested and len(relevant) < 3:
                    seen = {mission.mission_id for mission in relevant}
                    relevant.extend(
                        mission for mission in self.missions.store.list(limit=6)
                        if mission.mission_id not in seen and mission.state != "cancelled"
                    )
            elif continuity_requested:
                relevant = self.missions.store.list(states=active_states, limit=3)
                if not relevant:
                    relevant = [
                        mission for mission in self.missions.store.list(limit=3)
                        if mission.state != "cancelled"
                    ]
            else:
                relevant = []
            mission_used = 0
            for mission in relevant[:3]:
                completed = mission.checkpoint.get("completed_steps", [])
                completed_count = len(completed) if isinstance(completed, list) else 0
                last_result = mission.checkpoint.get("last_result")
                compact_result = ""
                if isinstance(last_result, dict):
                    # Persisted mission checkpoints are already stripped of file
                    # bodies/diffs.  Keep only a tiny deterministic roll-up here.
                    fields = []
                    for key in ("capability_id", "status", "verification_status", "error_category"):
                        value = last_result.get(key)
                        if value is not None:
                            fields.append(f"{key}={str(value)[:120]}")
                    if fields:
                        compact_result = " | last=" + ",".join(fields)
                        compact_result = compact_result[:MISSION_RESULT_BUDGET_CHARS]
                summary = (f"[MISSION] {mission.objective[:500]} | state={mission.state} | "
                           f"progress={mission.current_step}/{mission.step_count} | completed={completed_count}"
                           f"{compact_result}"
                           f"{(' | last_error=' + str(mission.last_error_category)[:120]) if mission.last_error_category else ''}\n")
                if mission_used + len(summary) > MISSION_BUDGET_CHARS:
                    continue
                if sum(map(len, chunks)) + len(summary) > MAX_CONTEXT_CHARS:
                    break
                chunks.append(summary)
                mission_used += len(summary)
                diagnostics.append({
                    "source": "mission", "reference": mission.mission_id,
                    "reasons": ["same conversation" if mission.conversation_id == conversation_id else "continuity request"],
                })

        if self.project_brain is not None and self.project_brain.should_target(request):
            if self.project_brain.status()["status"] != "ready":
                self.project_brain.refresh()
            target = self.project_brain.target(request)
            header = "[PROJECT KNOWLEDGE; untrusted repository data]\n"
            remaining = max(0, MAX_CONTEXT_CHARS - sum(map(len, chunks)))
            project_budget = min(PROJECT_BUDGET_CHARS, remaining)
            project = target.context[:max(0, project_budget - len(header))]
            if project:
                chunks.append(header + project)
                diagnostics.extend(
                    {"source": "project", "reference": path, "reasons": [reason]}
                    for path, reason in zip(target.relevant_files, target.reasons)
                )

        content = "".join(chunks)[:MAX_CONTEXT_CHARS]
        return ContextPackage(content, len(content), tuple(diagnostics), tuple(ids))

    @staticmethod
    def _memory_line(result: RetrievedMemory) -> str:
        item = result.item
        return (f"[MEMORY {item.memory_type}; provenance={item.provenance}; confidence={item.confidence:.2f}] "
                f"{item.subject}: {item.content}\n")
