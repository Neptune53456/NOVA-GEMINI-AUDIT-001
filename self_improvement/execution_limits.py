"""Trusted, serializable contract for a supervised worker (no provider access)."""
from dataclasses import asdict, dataclass
import difflib
import math
from pathlib import PurePosixPath
import time


@dataclass(frozen=True)
class ExecutionLimits:
    max_tasks: int
    max_source_files: int
    max_diff_lines: int
    max_model_calls: int
    deadline_monotonic: float
    rollback_reserve_seconds: float = 30.0

    def __post_init__(self):
        for name, maximum in (("max_tasks", 1), ("max_source_files", 100),
                              ("max_diff_lines", 10000), ("max_model_calls", 500)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"unsupported_execution_limits: {name}")
        for name in ("deadline_monotonic", "rollback_reserve_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"unsupported_execution_limits: {name}")

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("unsupported_execution_limits: missing or unknown fields")
        return cls(**raw)

    def to_dict(self):
        return asdict(self)

    def remaining(self):
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def phase_timeout(self, maximum, *, minimum=1.0, future_seconds=0.0):
        available = self.remaining() - self.rollback_reserve_seconds - future_seconds
        if available < minimum:
            raise TimeoutError("INSUFFICIENT_GLOBAL_TIME")
        return min(float(maximum), available)


def is_test_path(path):
    p = PurePosixPath(str(path).replace("\\", "/"))
    return p.name.startswith("test_") or p.name.endswith("_test.py") or "tests" in p.parts


def check_candidate(changes, max_source_files, max_diff_lines):
    """Compare the complete candidate with its immutable baseline, not a retry."""
    changed = {str(p): (before, after) for p, (before, after) in changes.items() if before != after}
    source_count = sum(not is_test_path(p) for p in changed)
    if source_count > max_source_files:
        raise ValueError("candidate_source_file_limit_exceeded")
    lines = 0
    for before, after in changed.values():
        # Binary/non-UTF8 changes cannot be admitted under a textual diff cap.
        if isinstance(before, bytes):
            before = before.decode("utf-8")
        if isinstance(after, bytes):
            after = after.decode("utf-8")
        for tag, i, j, k, l in difflib.SequenceMatcher(
                a=before.splitlines(keepends=True), b=after.splitlines(keepends=True), autojunk=False).get_opcodes():
            if tag != "equal":
                lines += j - i + l - k
    if lines > max_diff_lines:
        raise ValueError("candidate_diff_limit_exceeded")
    return {"source_files": source_count, "diff_lines": lines}
