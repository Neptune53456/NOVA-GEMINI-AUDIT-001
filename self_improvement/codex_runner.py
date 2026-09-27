"""Adaptateur borné pour l'exécution non interactive de Codex CLI."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time


MAX_PROMPT_CHARS = 24_000
MAX_OUTPUT_CHARS = 40_000
CODEX_CLI_ENV = "CODEX_CLI_PATH"
CODEX_VERSION_TIMEOUT_SECONDS = 10
DEFAULT_CODEX_TIMEOUT_SECONDS = 300
DEFAULT_HEARTBEAT_SECONDS = 10.0
DEFAULT_SILENCE_WARNING_SECONDS = 60.0
PROCESS_POLL_SECONDS = 0.2
PROCESS_STOP_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class CodexRunResult:
    success: bool
    returncode: int | None
    stdout: str
    stderr: str
    command: list[str]
    error: str | None = None
    timed_out: bool = False
    interrupted: bool = False
    duration_seconds: float = 0.0


class _BoundedTextBuffer:
    """Tampon thread-safe qui ne conserve que la fin de la sortie."""

    def __init__(self, maximum_chars=MAX_OUTPUT_CHARS):
        self.maximum_chars = maximum_chars
        self._value = ""
        self._lock = threading.Lock()

    def append(self, value: str):
        with self._lock:
            self._value = (self._value + value)[-self.maximum_chars:]

    def get(self) -> str:
        with self._lock:
            return self._value


class CodexRunner:
    def __init__(
        self, *, executable=None, finder=shutil.which,
        process_runner=subprocess.run, popen_factory=subprocess.Popen,
        environ=None, platform=None, clock=time.monotonic, sleeper=time.sleep,
        process_tree_killer=None,
    ):
        environment = os.environ if environ is None else environ
        self.platform = platform or os.name
        self.executable = executable or environment.get(CODEX_CLI_ENV) or self._find_executable(
            finder, self.platform
        )
        self.process_runner = process_runner
        self.popen_factory = popen_factory
        self.clock = clock
        self.sleeper = sleeper
        self.process_tree_killer = process_tree_killer or self._kill_process_tree

    @staticmethod
    def _find_executable(finder, platform: str):
        # Sur Windows, cibler explicitement le lanceur npm .CMD sans shell=True.
        names = ("codex.cmd", "codex") if platform == "nt" else ("codex",)
        for name in names:
            executable = finder(name)
            if executable:
                return executable
        return None

    @property
    def available(self) -> bool:
        return bool(self.executable)

    def build_command(self, workspace: Path | str) -> list[str]:
        workspace = Path(workspace).resolve()
        return [
            str(self.executable), "exec", "--ephemeral", "--sandbox", "workspace-write",
            "--color", "never", "-C", str(workspace), "-",
        ]

    def detect_version(self) -> tuple[int, int, int] | None:
        """Retourne la version installée sans supposer une CLI plus récente."""
        if not self.available:
            return None
        try:
            completed = self.process_runner(
                [str(self.executable), "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=CODEX_VERSION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode:
            return None
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", completed.stdout or "")
        return tuple(map(int, match.groups())) if match else None

    def _popen_options(self, workspace: Path) -> dict:
        options = {
            "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1,
            "cwd": workspace,
        }
        if self.platform == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        return options

    @staticmethod
    def _read_stream(stream, output: _BoundedTextBuffer, activity, activity_lock, clock):
        try:
            for line in iter(stream.readline, ""):
                output.append(line)
                with activity_lock:
                    activity[0] = clock()
        finally:
            try:
                stream.close()
            except (AttributeError, OSError):
                pass

    def _kill_process_tree(self, process):
        """Arrête uniquement l'arbre créé pour cette invocation Codex."""
        if process.poll() is not None:
            return
        try:
            if self.platform == "nt" and getattr(process, "pid", None):
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=10,
                )
            elif getattr(process, "pid", None):
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=PROCESS_STOP_GRACE_SECONDS)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
                process.wait(timeout=PROCESS_STOP_GRACE_SECONDS)
            except (OSError, subprocess.SubprocessError):
                pass

    def run(
        self, task: str, workspace: Path | str,
        *, timeout_seconds=DEFAULT_CODEX_TIMEOUT_SECONDS,
        heartbeat_seconds=DEFAULT_HEARTBEAT_SECONDS,
        silence_warning_seconds=DEFAULT_SILENCE_WARNING_SECONDS, logger=print,
    ) -> CodexRunResult:
        workspace = Path(workspace).resolve()
        if not self.available:
            return CodexRunResult(False, None, "", "", [], "Codex CLI introuvable. Installez et authentifiez Codex CLI explicitement.")
        if not workspace.is_dir() or not (workspace / ".git").exists():
            return CodexRunResult(False, None, "", "", [], "Le workspace Codex doit être un worktree Git existant.")
        if not isinstance(task, str) or not task.strip() or len(task) > MAX_PROMPT_CHARS:
            return CodexRunResult(False, None, "", "", [], "Tâche Codex vide ou trop volumineuse.")
        if timeout_seconds <= 0 or heartbeat_seconds <= 0 or silence_warning_seconds <= 0:
            return CodexRunResult(False, None, "", "", [], "Les délais Codex doivent être strictement positifs.")

        command = self.build_command(workspace)
        started = self.clock()
        stdout = _BoundedTextBuffer()
        stderr = _BoundedTextBuffer()
        activity = [started]
        activity_lock = threading.Lock()
        process = None
        threads = []
        timed_out = interrupted = False

        try:
            process = self.popen_factory(command, **self._popen_options(workspace))
            process.stdin.write(task)
            process.stdin.close()
            for stream, target in ((process.stdout, stdout), (process.stderr, stderr)):
                thread = threading.Thread(
                    target=self._read_stream,
                    args=(stream, target, activity, activity_lock, self.clock),
                    daemon=True,
                )
                thread.start()
                threads.append(thread)

            next_heartbeat = started + heartbeat_seconds
            next_silence_warning = started + silence_warning_seconds
            while process.poll() is None:
                now = self.clock()
                if now - started >= timeout_seconds:
                    timed_out = True
                    logger(f"[SelfImprove] Timeout Codex après {timeout_seconds:g} secondes - arrêt du processus")
                    self.process_tree_killer(process)
                    break
                if now >= next_heartbeat:
                    logger(f"[SelfImprove] Codex en cours depuis {int(now - started)} secondes...")
                    next_heartbeat += heartbeat_seconds
                with activity_lock:
                    silent_for = now - activity[0]
                if now >= next_silence_warning and silent_for >= silence_warning_seconds:
                    logger(f"[SelfImprove] Codex ne produit aucune sortie depuis {int(silent_for)} secondes")
                    next_silence_warning = now + silence_warning_seconds
                self.sleeper(PROCESS_POLL_SECONDS)
        except KeyboardInterrupt:
            interrupted = True
            logger("[SelfImprove] Interruption demandée - arrêt de Codex CLI")
            if process is not None:
                self.process_tree_killer(process)
        except OSError as error:
            if process is not None:
                self.process_tree_killer(process)
            duration = round(self.clock() - started, 3)
            return CodexRunResult(False, None, stdout.get(), stderr.get(), command, f"Impossible de lancer Codex : {error}", duration_seconds=duration)
        finally:
            for thread in threads:
                thread.join(timeout=1)

        returncode = process.poll() if process is not None else None
        duration = round(self.clock() - started, 3)
        logger(f"[SelfImprove] Codex terminé - exit code {returncode} - durée {duration:.1f} s")
        success = returncode == 0 and not timed_out and not interrupted
        if timed_out:
            error = f"Codex a dépassé le délai de {timeout_seconds:g} s."
        elif interrupted:
            error = "Exécution Codex interrompue par l'utilisateur."
        elif success:
            error = None
        else:
            error = "Codex CLI a signalé un échec."
        return CodexRunResult(
            success, returncode, stdout.get(), stderr.get(), command, error,
            timed_out, interrupted, duration,
        )
