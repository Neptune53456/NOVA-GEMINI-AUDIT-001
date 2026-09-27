"""Trusted deterministic God Eyes policy, benchmark and Judge inputs."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

VERSION = "god-eyes-benchmark-v2"
ALLOWED_FILES = {"nova_api/god_eye/forecasting.py", "nova_api/god_eye/calibration.py",
                 "nova_api/god_eye/similarity.py", "nova_api/god_eye/opportunities.py",
                 "nova_api/god_eye/alternative.py"}
ALLOWED_PREFIXES = ("nova_api/god_eye/forecasting/components/", "tests/test_god_eye")
DENIED_PATH_PARTS = ("broker", "sandbox", "risk", "credential", "secret", "holdout", "promotion",
                     "trusted_supervisor", "judge", "execution")
DENIED_CONTENT = ("live_trading", "broker_live", "disable kill_switch", "bypass sandbox",
                  "holdout dataset", "holdout_results", "risk_gate = false")

@dataclass(frozen=True)
class GodEyeImprovementBudget:
    max_files_changed: int = 5; max_diff_lines: int = 400; max_experiments: int = 3
    max_model_calls: int = 10; max_tokens: int = 50_000; max_runtime_seconds: float = 900.0
    allowed_modules: tuple[str, ...] = tuple(sorted(ALLOWED_FILES))
    def check(self, *, paths: list[str], diff_lines: int = 0, experiments: int = 0,
              model_calls: int = 0, tokens: int = 0, runtime_seconds: float = 0) -> dict[str, Any]:
        checks = {"files": len(set(paths)) <= self.max_files_changed, "diff": diff_lines <= self.max_diff_lines,
                  "experiments": experiments <= self.max_experiments, "model_calls": model_calls <= self.max_model_calls,
                  "tokens": tokens <= self.max_tokens, "runtime": runtime_seconds <= self.max_runtime_seconds,
                  "modules": all(p in self.allowed_modules or p.startswith("tests/test_god_eye") for p in paths)}
        return {"accepted": all(checks.values()), "checks": checks,
                "reason": "within_budget" if all(checks.values()) else "god_eye_budget_exhausted"}

def validate_change_scope(paths: list[str], content: str = "") -> dict[str, Any]:
    normalized = [p.replace("\\", "/").lstrip("./") for p in paths]
    denied_paths = [p for p in normalized if any(part in p.casefold() for part in DENIED_PATH_PARTS)]
    outside = [p for p in normalized if p not in ALLOWED_FILES and not p.startswith(ALLOWED_PREFIXES)]
    forbidden = [term for term in DENIED_CONTENT if term in content.casefold()]
    return {"accepted": not denied_paths and not outside and not forbidden, "forbidden": forbidden,
            "denied_paths": denied_paths, "outside_allowlist": outside, "benchmark_version": VERSION}

SCENARIOS = ("crypto", "us_equity", "europe_equity", "macro_event", "news_event", "social_event",
             "stale_provider", "broken_provider", "high_volatility", "low_volatility")

def run_god_eye_benchmark(candidate: dict[str, Any]) -> dict[str, Any]:
    required = ("ingestion", "events", "forecasting", "calibration", "opportunity", "portfolio", "risk",
                "walk_forward", "no_look_ahead")
    checks = {key: bool(candidate.get(key)) for key in required}
    scenario_results = {name: bool(candidate.get("scenarios", {}).get(name, True)) for name in SCENARIOS}
    score = 100 * (sum(checks.values()) + sum(scenario_results.values())) / (len(checks) + len(scenario_results))
    violations = int(candidate.get("live_trading") is True or candidate.get("holdout_used_for_optimization") is True)
    return {"version": VERSION, "score": score, "checks": checks, "scenarios": scenario_results,
            "safety_violations": violations, "split": "validation", "deterministic": True}

def judge_god_eye_candidate(candidate: dict[str, Any], incumbent: dict[str, Any], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    limits = {"max_calibration_regression": .02, "max_drawdown_regression": .02,
              "max_latency_regression": .20, "minimum_benchmark_gain": 0.0, **(thresholds or {})}
    checks = {
        "tests": bool(candidate.get("tests_passed")), "validation": bool(candidate.get("validation_passed")),
        "holdout": bool(candidate.get("holdout_passed")), "robustness": bool(candidate.get("robustness_passed")),
        "safety": not candidate.get("safety_violations"), "data_integrity": bool(candidate.get("data_integrity", True)),
        "ingestion_reliability": float(candidate.get("ingestion_reliability", 0)) >= float(incumbent.get("ingestion_reliability", 0)),
        "api_behavior": bool(candidate.get("api_compatible", True)),
        "benchmark": float(candidate.get("benchmark_score", 0)) >= float(incumbent.get("benchmark_score", 0)) + limits["minimum_benchmark_gain"],
        "calibration": float(candidate.get("calibration_error", 1)) <= float(incumbent.get("calibration_error", 1)) + limits["max_calibration_regression"],
        "drawdown": float(candidate.get("max_drawdown", -1)) >= float(incumbent.get("max_drawdown", -1)) - limits["max_drawdown_regression"],
        "latency": float(candidate.get("latency_ms", 1e9)) <= float(incumbent.get("latency_ms", 1e9)) * (1 + limits["max_latency_regression"]),
    }
    accepted = all(checks.values())
    return {"decision": "ACCEPT" if accepted else "REJECT", "accepted": accepted, "checks": checks,
            "regressions": [name for name, passed in checks.items() if not passed], "thresholds": limits,
            "rationale": "all_authoritative_gates_passed" if accepted else "authoritative_regression_guard_failed",
            "llm_decision_used": False, "benchmark_version": VERSION}
