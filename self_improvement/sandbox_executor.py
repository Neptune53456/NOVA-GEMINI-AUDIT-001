"""Exécution bornée des tests candidats, avec Docker si disponible.

Le mode ``auto`` préfère Docker et retombe explicitement sur le sous-processus local
sanitisé de V5. Le conteneur est éphémère, sans réseau, avec limites CPU/RAM/PIDs.
Ce module fait partie du control-plane : le code candidat ne choisit jamais ses
propres privilèges.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import os, shutil, subprocess, sys
from typing import Mapping, Sequence

from self_improvement.process_safety import sanitized_child_environment


def _timeout_text(value) -> str:
    """Normalize TimeoutExpired output across Python/platform variants."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float = 180.0
    memory_mb: int = 1024
    cpus: float = 1.5
    pids: int = 256


@dataclass
class SandboxResult:
    returncode: int | None
    stdout: str
    stderr: str
    backend: str
    command: list[str]
    timed_out: bool = False

    def to_dict(self): return asdict(self)


class SandboxExecutor:
    def __init__(self, repo_root: str | Path, *, mode: str = "auto", image: str | None = None,
                 limits: SandboxLimits | None = None):
        self.repo_root = Path(repo_root).resolve()
        self.mode = (mode or os.getenv("PROJET_IA_SANDBOX", "auto")).casefold()
        if self.mode not in {"auto", "docker", "local"}:
            raise ValueError("sandbox_mode_invalid")
        self.image = image or os.getenv("PROJET_IA_SANDBOX_IMAGE", "projet-ia-sandbox:py312")
        self.limits = limits or SandboxLimits()

    @staticmethod
    def docker_available() -> bool:
        docker = shutil.which("docker")
        if not docker: return False
        try:
            p = subprocess.run([docker, "info"], capture_output=True, text=True, timeout=8,
                               env=sanitized_child_environment())
            return p.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def docker_image_available(self) -> bool:
        if not self.docker_available(): return False
        try:
            p = subprocess.run(["docker", "image", "inspect", self.image], capture_output=True, text=True, timeout=8, env=sanitized_child_environment())
            return p.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @property
    def backend(self) -> str:
        if self.mode == "local": return "local"
        if self.docker_image_available(): return "docker"
        if self.mode == "docker": raise RuntimeError("docker_sandbox_required_but_unavailable_or_image_missing")
        return "local"

    def run(self, command: Sequence[str], *, env: Mapping[str, str] | None = None) -> SandboxResult:
        cmd = [str(x) for x in command]
        return self._run_docker(cmd, env=env) if self.backend == "docker" else self._run_local(cmd, env=env)

    def _run_local(self, command: list[str], *, env=None) -> SandboxResult:
        child_env = sanitized_child_environment(extra=env or {})
        try:
            p = subprocess.run(command, cwd=self.repo_root, capture_output=True, text=True,
                               timeout=self.limits.timeout_seconds, env=child_env)
            return SandboxResult(p.returncode, p.stdout, p.stderr, "local", command)
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(None, _timeout_text(exc.stdout), _timeout_text(exc.stderr), "local", command, True)

    def _run_docker(self, command: list[str], *, env=None) -> SandboxResult:
        # Le conteneur utilise son propre Python. On convertit l'interpréteur hôte en `python`.
        inner = list(command)
        if inner and Path(inner[0]).name.casefold().startswith("python"):
            inner[0] = "python"
        docker_cmd = [
            "docker", "run", "--rm", "--network", "none",
            "--memory", f"{max(128, self.limits.memory_mb)}m",
            "--cpus", str(max(0.25, self.limits.cpus)),
            "--pids-limit", str(max(32, self.limits.pids)),
            "--security-opt", "no-new-privileges:true",
            "--cap-drop", "ALL",
            "-v", f"{self.repo_root}:/workspace",
            "-w", "/workspace",
        ]
        for key, value in (env or {}).items():
            if key and value is not None:
                docker_cmd += ["-e", f"{key}={value}"]
        docker_cmd += [self.image, *inner]
        try:
            p = subprocess.run(docker_cmd, capture_output=True, text=True,
                               timeout=self.limits.timeout_seconds, env=sanitized_child_environment())
            return SandboxResult(p.returncode, p.stdout, p.stderr, "docker", docker_cmd)
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(None, _timeout_text(exc.stdout), _timeout_text(exc.stderr), "docker", docker_cmd, True)
