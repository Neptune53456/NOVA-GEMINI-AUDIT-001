"""Small stoppable ingestion scheduler; disabled until explicitly started."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Callable, Mapping


@dataclass
class TaskState:
    interval_seconds: float
    failures: int = 0
    next_run_at: datetime | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_status: str = "never"
    last_error: str | None = None
    lane: str = "ingestion"
    running: bool = False
    started_monotonic: float | None = None


class GodEyeScheduler:
    def __init__(self, market_refresh: Callable[[], object], news_refresh: Callable[[], object], *,
                 market_interval: float = 60.0, news_interval: float = 300.0,
                 evaluation: Callable[[], object] | None = None, evaluation_interval: float = 300.0,
                 max_backoff: float = 3600.0, clock: Callable[[], datetime] | None = None,
                 extra_tasks: Mapping[str, tuple[Callable[[], object], float]] | None = None,
                 initial_state: Mapping[str, dict[str, object]] | None = None,
                 save_state: Callable[[dict[str, object]], None] | None = None,
                 task_lanes: Mapping[str, str] | None = None,
                 task_timeout: float = 120.0) -> None:
        self.actions = {"market": market_refresh, "news": news_refresh}
        self.tasks = {"market": TaskState(max(1.0, market_interval)),
                      "news": TaskState(max(1.0, news_interval))}
        if evaluation:
            self.actions["evaluation"] = evaluation
            self.tasks["evaluation"] = TaskState(max(1.0, evaluation_interval))
        for name, (action, interval) in (extra_tasks or {}).items():
            if name in self.tasks: raise ValueError("duplicate_scheduler_task")
            self.actions[name], self.tasks[name] = action, TaskState(max(10.0, float(interval)))
        self.max_backoff = max_backoff
        lanes = {"market": "trading", "news": "ingestion", "evaluation": "critical",
                 "star_finder": "trading", "paper_positions": "critical",
                 "outcome_resolution": "critical"}
        lanes.update(task_lanes or {})
        for name, state in self.tasks.items():
            lane = lanes.get(name, "ingestion")
            if lane not in {"critical", "trading", "ingestion"}: raise ValueError("invalid_scheduler_lane")
            state.lane = lane
        self.task_timeout = max(1.0, float(task_timeout))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.save_state = save_state
        for name, value in (initial_state or {}).items():
            if name not in self.tasks: continue
            state = self.tasks[name]
            state.failures = max(0, int(value.get("failures", 0)))
            state.last_status = str(value.get("last_status", "never"))
            state.last_error = str(value["last_error"]) if value.get("last_error") else None
            for field in ("next_run_at", "last_started_at", "last_finished_at"):
                raw = value.get(field)
                if raw: setattr(state, field, datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        self._stop, self._run_lock, self._state_lock = Event(), Lock(), Lock()
        self._thread: Thread | None = None
        self._queues = {name: Queue(maxsize=max(1, sum(s.lane == name for s in self.tasks.values())))
                        for name in ("critical", "trading", "ingestion")}
        self._workers: dict[str, Thread] = {}
        self.started_at: datetime | None = None
        self.last_loop_at: datetime | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if any(worker.is_alive() for worker in self._workers.values()):
            return  # fail closed: never duplicate a worker still blocked in provider code
        self._stop.clear()
        self.started_at = self.clock()
        self._workers = {}
        for lane in ("critical", "trading", "ingestion"):
            worker = Thread(target=self._worker, args=(lane,), name=f"nova-god-eye-{lane}", daemon=True)
            self._workers[lane] = worker; worker.start()
        self._thread = Thread(target=self._loop, name="nova-god-eye-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        deadline = timeout / max(1, len(self._workers))
        for worker in self._workers.values(): worker.join(deadline)
        for queue in self._queues.values():
            while True:
                try:
                    _, state, _ = queue.get_nowait()
                    with self._state_lock: state.running = False
                    queue.task_done()
                except Empty: break

    def run_due(self) -> bool:
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            now = self.clock()
            for name, state in self.tasks.items():
                if state.next_run_at is None or state.next_run_at <= now:
                    if self._thread and self._thread.is_alive(): self._dispatch(name, state, now)
                    else: self._run(name, state, now)
            return True
        finally:
            self._run_lock.release()

    def _run(self, name: str, state: TaskState, now: datetime) -> None:
        with self._state_lock:
            if state.running: return
            state.running = True; state.started_monotonic = monotonic(); state.last_started_at = now; state.last_finished_at = None
        try:
            result = self.actions[name]()
            failed = isinstance(result, dict) and result.get("status") == "error"
            if failed:
                raise RuntimeError("refresh_status_error")
            state.failures, state.last_status = 0, "success"
            state.last_error = None
            delay = state.interval_seconds
        except Exception as error:
            state.failures += 1
            state.last_status = "error"
            state.last_error = f"{type(error).__name__}:{error}"[:240]
            # Persist the complete diagnostic count, but never exponentiate it directly.
            exponent = min(state.failures, 30)
            try:
                delay = min(self.max_backoff, state.interval_seconds * (2 ** exponent))
            except (OverflowError, ValueError):
                delay = self.max_backoff
        finished = self.clock()
        with self._state_lock:
            state.last_finished_at = finished
            state.next_run_at = now + timedelta(seconds=delay)
            state.running = False; state.started_monotonic = None
        if self.save_state:
            try: self.save_state(self.status())
            except Exception: pass  # state persistence must not kill ingestion

    def _dispatch(self, name: str, state: TaskState, now: datetime) -> None:
        with self._state_lock:
            if state.running: return
            state.running = True; state.started_monotonic = monotonic(); state.last_started_at = now; state.last_finished_at = None
        try:
            self._queues[state.lane].put_nowait((name, state, now))
        except Full:
            with self._state_lock:
                state.running = False; state.last_status = "deferred"; state.last_error = "lane_queue_full"

    def _worker(self, lane: str) -> None:
        queue = self._queues[lane]
        while not self._stop.is_set():
            try: name, state, now = queue.get(timeout=.2)
            except Empty: continue
            # _run owns the guard for direct calls; dispatched work already owns it.
            with self._state_lock: state.running = False
            self._run(name, state, now)
            queue.task_done()

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            self.last_loop_at = self.clock()
            try:
                self.run_due()
            except Exception:
                # A scheduling defect must be observable, but must not silently kill the thread.
                continue

    def status(self) -> dict[str, object]:
        alive = bool(self._thread and self._thread.is_alive())
        timed_out = {name for name,state in self.tasks.items() if state.running and state.started_monotonic is not None
                     and monotonic()-state.started_monotonic >= self.task_timeout}
        return {"running": alive, "scheduler_alive": alive,
                "lanes": {name: worker.is_alive() for name,worker in self._workers.items()},
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "last_loop_at": self.last_loop_at.isoformat() if self.last_loop_at else None,
                "tasks": {name: {"interval_seconds": state.interval_seconds,
                                  "failures": state.failures,
                                  "next_run_at": state.next_run_at.isoformat() if state.next_run_at else None,
                                  "last_started_at": state.last_started_at.isoformat() if state.last_started_at else None,
                                  "last_finished_at": state.last_finished_at.isoformat() if state.last_finished_at else None,
                                  "last_status": state.last_status,
                                  "last_error": state.last_error,
                                  "lane": state.lane,
                                  "running": state.running,
                                  "timed_out": name in timed_out,
                                  "timeout_seconds": self.task_timeout}
                          for name, state in self.tasks.items()}}
