"""Bounded, sanitized collection for the local read-only cockpit."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from .schemas import (
    CockpitAlert,
    CockpitApiStatus,
    CockpitResponse,
    ComponentStatusItem,
    ProjectStatus,
    StateResponse,
    ValidationSummary,
)

StateSource = Callable[[], StateResponse | Mapping[str, Any]]
PROJECT_ROOT = Path(__file__).resolve().parent.parent
GIT_TIMEOUT_SECONDS = 1.0


class CockpitSource(Protocol):
    """Injectable producer used by the HTTP route and deterministic tests."""

    def snapshot(self) -> CockpitResponse | Mapping[str, Any]: ...


def _git_status() -> ProjectStatus:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--branch"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
        if result.returncode != 0:
            raise RuntimeError("git status unavailable")
        lines = result.stdout.splitlines()
        branch = None
        entries = lines
        if lines and lines[0].startswith("## "):
            branch = lines[0][3:].split("...", 1)[0].strip() or None
            entries = lines[1:]
        untracked = sum(line.startswith("??") for line in entries)
        modified = sum(not line.startswith("??") for line in entries)
        return ProjectStatus(
            git_available=True,
            branch=branch,
            clean=not entries,
            modified_count=modified,
            untracked_count=untracked,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return ProjectStatus(
            git_available=False,
            branch=None,
            clean=None,
            modified_count=None,
            untracked_count=None,
        )


class LocalCockpitSource:
    """Collect a cheap snapshot and cache it briefly for frontend polling."""

    def __init__(self, state_source: StateSource, cache_seconds: float = 2.0) -> None:
        self._state_source = state_source
        self._cache_seconds = cache_seconds
        self._started_at = time.monotonic()
        self._cached_at = 0.0
        self._cached: CockpitResponse | None = None
        self._lock = Lock()

    def snapshot(self) -> CockpitResponse:
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now - self._cached_at < self._cache_seconds:
                return self._cached
            snapshot = self._collect(now)
            self._cached = snapshot
            self._cached_at = now
            return snapshot

    def _collect(self, now: float) -> CockpitResponse:
        nova_source_failed = False
        try:
            state_value = self._state_source()
            nova = state_value if isinstance(state_value, StateResponse) else StateResponse(**dict(state_value))
        except Exception:
            nova_source_failed = True
            nova = StateResponse(
                state="error",
                label="État indisponible",
                busy=False,
                message="La source d’état Nova est indisponible.",
            )
        project = _git_status()
        engine_status = "unavailable" if nova.state == "error" else "available"
        components = [
            ComponentStatusItem(id="nova-api", label="API locale", status="available", message="Opérationnelle"),
            ComponentStatusItem(id="web-ui", label="Interface web", status="unknown", message="État non vérifiable depuis l’API"),
            ComponentStatusItem(id="nova-engine", label="Moteur Nova", status=engine_status, message=nova.message),
            ComponentStatusItem(id="self-improvement", label="Auto-amélioration", status="unknown", message="Aucune vérification active"),
            ComponentStatusItem(id="safety-control", label="Contrôle de sécurité", status="unknown", message="Aucune vérification active"),
            ComponentStatusItem(id="model-provider", label="Fournisseur de modèle", status="unknown", message="Aucun appel de vérification effectué"),
        ]
        alerts = [CockpitAlert(level="info", message="Aucune validation récente connue.")]
        if not project.git_available:
            alerts.insert(0, CockpitAlert(level="warning", message="État Git indisponible."))
        elif not project.clean:
            alerts.insert(0, CockpitAlert(level="warning", message="Le dépôt Git contient des modifications."))
        if nova_source_failed:
            alerts.insert(0, CockpitAlert(level="error", message="Source d’état Nova indisponible."))
        elif nova.state == "error":
            alerts.insert(0, CockpitAlert(level="error", message="Nova signale une erreur."))
        return CockpitResponse(
            generated_at=datetime.now(timezone.utc).isoformat(),
            api=CockpitApiStatus(uptime_seconds=max(0, int(now - self._started_at))),
            nova=nova,
            project=project,
            components=components,
            validation=ValidationSummary(status="unknown", message="Aucune validation récente disponible."),
            alerts=alerts,
        )
