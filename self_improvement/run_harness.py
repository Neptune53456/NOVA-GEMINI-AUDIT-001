"""Reliable subprocess harness for observable project and REAL runs."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Sequence


@dataclass
class RunExecutionReport:
    run_id: str
    command: list[str]
    start_time: str
    end_time: str | None = None
    duration_seconds: float = 0.0
    pid: int | None = None
    exit_code: int | None = None
    timed_out: bool = False
    stdout_path: str = ""
    stderr_path: str = ""
    artifact_path: str | None = None
    heartbeat_path: str | None = None
    process_tree: list[int] = field(default_factory=list)
    exception: str | None = None
    completed: bool = False
    success: bool = False
    environment: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunHarness:
    """Run an existing entrypoint and persist all execution evidence."""

    def __init__(self, repo_root: str | Path, reports_root: str | Path | None = None):
        self.repo_root = Path(repo_root).resolve()
        self.reports_root = (Path(reports_root) if reports_root else self.repo_root / "self_improvement" / "reports" / "runtime").resolve()
        self.reports_root.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        artifact_path: str | Path | None = None,
        heartbeat_seconds: float = 30.0,
        environment: dict[str, str] | None = None,
        summary_kind: str | None = None,
    ) -> RunExecutionReport:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        stdout_path = self.reports_root / f"{run_id}.stdout.log"
        stderr_path = self.reports_root / f"{run_id}.stderr.log"
        heartbeat_path = self.reports_root / f"{run_id}.heartbeat.json"
        resolved_artifact = Path(artifact_path).resolve() if artifact_path else None
        if resolved_artifact:
            resolved_artifact.parent.mkdir(parents=True, exist_ok=True)
        command_list = [str(item) for item in command]
        started_at = datetime.now(timezone.utc)
        report = RunExecutionReport(
            run_id=run_id,
            command=command_list,
            start_time=started_at.isoformat(),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            artifact_path=str(resolved_artifact) if resolved_artifact else None,
            heartbeat_path=str(heartbeat_path),
            environment=self._environment_snapshot(),
        )
        env = os.environ.copy()
        env.update(environment or {})
        env["PYTHONUNBUFFERED"] = "1"
        process: subprocess.Popen[str] | None = None
        stop_heartbeat = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.wait(max(1.0, heartbeat_seconds)):
                self._write_heartbeat(heartbeat_path, report, process)

        heartbeat_thread = threading.Thread(target=heartbeat, name=f"{run_id}-heartbeat", daemon=True)
        try:
            with stdout_path.open("w", encoding="utf-8", buffering=1) as stdout, stderr_path.open("w", encoding="utf-8", buffering=1) as stderr:
                kwargs: dict[str, Any] = {
                    "cwd": str(self.repo_root), "env": env, "stdout": stdout, "stderr": stderr,
                    "text": True, "bufsize": 1,
                }
                if os.name == "nt":
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                else:
                    kwargs["start_new_session"] = True
                process = subprocess.Popen(command_list, **kwargs)
                report.pid = process.pid
                report.process_tree = [process.pid]
                heartbeat_thread.start()
                try:
                    process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    report.timed_out = True
                    self._terminate_process_tree(process)
                    process.wait(timeout=10)
                report.exit_code = process.returncode
        except Exception as exc:
            report.exception = f"{type(exc).__name__}: {exc}"
            if process is not None and process.poll() is None:
                self._terminate_process_tree(process)
        finally:
            stop_heartbeat.set()
            if heartbeat_thread.ident is not None:
                heartbeat_thread.join(timeout=2)
            report.end_time = datetime.now(timezone.utc).isoformat()
            report.duration_seconds = round((datetime.now(timezone.utc) - started_at).total_seconds(), 3)
            report.completed = report.exit_code is not None and not report.timed_out and report.exception is None
            report.summary = self._summarize(report, summary_kind)
            report.success = report.completed and report.exit_code == 0 and self._artifact_is_valid(report.artifact_path, summary_kind)
            self._write_heartbeat(heartbeat_path, report, process, final=True)
        report_path = self.reports_root / f"{run_id}.json"
        report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report

    def _summarize(self, report: RunExecutionReport, kind: str | None) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        if kind == "suite" and Path(report.stdout_path).is_file():
            text = Path(report.stdout_path).read_text(encoding="utf-8", errors="replace")
            import re
            for line in reversed(text.splitlines()):
                counts = dict((kind, int(count)) for count, kind in re.findall(r"\b(\d+) (passed|failed)\b", line))
                if counts:
                    summary.update({"passed": counts.get("passed", 0), "failed": counts.get("failed", 0)})
                    break
            coverage = re.search(r"Total coverage:\s*(?P<coverage>\d+(?:\.\d+)?)%", text)
            if not coverage:
                coverage = re.search(r"^TOTAL\s+(?:\d+\s+){2,4}(?P<coverage>\d+(?:\.\d+)?)%", text, re.MULTILINE)
            if coverage:
                summary["coverage_percent"] = float(coverage.group("coverage"))
        if kind in {"real", "multicycle"} and report.artifact_path:
            try:
                payload = json.loads(Path(report.artifact_path).read_text(encoding="utf-8"))
                result = payload.get("result", payload)
                if isinstance(result, dict):
                    summary.update({key: result.get(key) for key in ("final_decision", "reason", "success", "total_cycles", "stop_reason", "total_model_calls") if key in result})
                    cycles = result.get("cycles") or result.get("cycle_results")
                    if isinstance(cycles, list):
                        summary["cycle_decisions"] = [item.get("decision") if isinstance(item, dict) else None for item in cycles]
            except (OSError, json.JSONDecodeError):
                summary["artifact_valid"] = False
        return summary

    @staticmethod
    def _artifact_is_valid(path: str | None, kind: str | None) -> bool:
        if not path:
            return True
        artifact = Path(path)
        if not artifact.is_file():
            return False
        try:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if kind in {"real", "multicycle"}:
            result = payload.get("result", payload) if isinstance(payload, dict) else None
            return isinstance(result, dict) and any(key in result for key in ("final_decision", "cycles", "cycle_results"))
        return True

    @staticmethod
    def _environment_snapshot() -> dict[str, Any]:
        return {
            "python_executable": sys.executable,
            "cwd": str(Path.cwd()),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "virtualenv": os.environ.get("VIRTUAL_ENV"),
            "flags": {key: os.environ.get(key) for key in ("PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE") if key in os.environ},
        }

    @staticmethod
    def _write_heartbeat(path: Path, report: RunExecutionReport, process: subprocess.Popen[str] | None, *, final: bool = False) -> None:
        payload = {
            "run_id": report.run_id, "pid": report.pid,
            "alive": bool(process and process.poll() is None), "final": final,
            "stdout_bytes": Path(report.stdout_path).stat().st_size if Path(report.stdout_path).exists() else 0,
            "stderr_bytes": Path(report.stderr_path).stat().st_size if Path(report.stderr_path).exists() else 0,
            "artifact_exists": bool(report.artifact_path and Path(report.artifact_path).is_file()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def _command_for(mode: str, repo_root: Path, artifact: Path, cycles: int) -> tuple[list[str], str | None]:
    if mode == "suite":
        # The repository config requests pytest-cov, which may be absent in a
        # minimal runtime. The harness must still execute the suite and report
        # the authoritative exit code rather than fail during argument parsing.
        return [sys.executable, "-u", "-m", "pytest", "-q", "-o", "addopts="], "suite"
    if mode == "real":
        return [sys.executable, "-u", "-m", "self_improvement.agent_runtime", "--output", str(artifact), "self-improve", "--cycles", "1"], "real"
    if mode == "multicycle":
        return [sys.executable, "-u", "-m", "self_improvement.agent_runtime", "--output", str(artifact), "self-improve", "--cycles", str(cycles)], "multicycle"
    raise ValueError(f"unknown mode: {mode}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("suite", "real", "multicycle"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--reports-root", type=Path)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--cycles", type=int, default=3)
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()
    harness = RunHarness(root, args.reports_root)
    artifact = harness.reports_root / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{args.mode}.json" if args.mode != "suite" else None
    command, kind = _command_for(args.mode, root, artifact or Path(""), args.cycles)
    report = harness.run(command, timeout_seconds=args.timeout, artifact_path=artifact, summary_kind=kind)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.success else (124 if report.timed_out else (report.exit_code if report.exit_code is not None else 1))


if __name__ == "__main__":
    raise SystemExit(main())
