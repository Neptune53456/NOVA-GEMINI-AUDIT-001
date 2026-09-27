# V1 hardening — Phase 1 baseline

Date: 2026-09-17  
Branch: `feature/nova-interface-foundation`

## Baseline and validated path

The concise machine-readable inventory is in `baseline.json`. It covers conversation/SSE, routing and fallback, AgentLoop, GoalRunner, missions, journal, transactions, memory/context/project grounding, filesystem/Git, computer/UIA/visual foundations, confirmations, and frontend conversation/cockpit surfaces. The registry currently exposes 22 capabilities.

The established real path is natural-language intent → planning and normalization → GoalRunner → UI confirmation → approved filesystem write → exact readback → `completed_verified`. The 24-scenario campaign in `scenarios.json` is catalogued but has not been executed.

## Metrics foundation

`nova_api.benchmark.BenchmarkRecorder` derives a privacy-safe result from authoritative goal fields and goal-correlated journal events, then appends JSONL locally. It never records objectives, prompts, responses, file contents, confirmation tokens, or raw provider bodies.

Available now are timing/status, verified success, model/action/replan/failure counts, confirmation requests/rejections, bounded error categories, and an explicit critical-failure flag. Approval count is derivable for verified completed goals. Token totals and local-only are emitted only when their source fields exist. Provider attempts/fallbacks, per-goal rollback, recovery count and restart-resume remain `null` because current events do not support safe attribution.

## UI state defect

Root cause: the SSE flow set the shared backend presentation state to `awaiting-confirmation`, while the frontend approved a goal through the direct goal-resume endpoint. That endpoint returned the final goal but did not update `ApiStateStore`, so cockpit polling kept showing the stale confirmation state.

The backend now owns one goal-status-to-presentation projection. Resume, pause, cancel, and conversation confirmation paths all apply it. It covers pending/running/acting/verifying, awaiting confirmation, paused, completed verified/unverified, blocked, failed, and cancelled. The frontend remains a renderer of backend authority; no duplicate frontend state machine was added.

## Capability-test disposition

The historical exact count of 18 was replaced by a structural contract: the response count must match its unique IDs and the required established capability subset must be present. Production registration was not reduced to satisfy history.

## Known risks and Phase 2 order

Cloud visual-provider validation remains externally dependent. Planner behavior remains provider-dependent within bounded schema normalization. Confirmation tokens remain process-local and expiry-sensitive. There is no frontend automated test framework. Provider quota/payment/cooldown can affect routing. Several desired metrics still require correlation IDs or explicit sanitized events.

Recommended Phase 2 order: (1) deterministic critical safety and state scenarios; (2) persistence/restart and rollback scenarios; (3) conversation isolation and cancellation; (4) mocked provider degradation; (5) real filesystem/UIA checks; (6) externally dependent provider/visual checks last. Record real results only when executed; do not pre-populate successes.

## Validation record

Targeted validation completed without running the full suite:

- Python: 57 targeted tests passed with `-q --no-cov`; two dependency deprecation warnings were reported.
- Frontend: `npm.cmd run lint` passed; `npm.cmd run build` passed (34 modules transformed).
- Python syntax: `py_compile` passed for the five modified production modules.
- Documents: both JSON artifacts parsed successfully.
- Diff hygiene: `git diff --check` passed; Git emitted only pre-existing LF/CRLF working-copy warnings for frontend files.
