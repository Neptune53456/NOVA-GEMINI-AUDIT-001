# Nova 1.4 Intelligence Foundation

## Scope

This milestone continues from `nova-ai-core-v1.3-perception-fusion-chatgpt.zip` and completes a bounded intelligence foundation without modifying the frozen Nova V1 evidence.

## Implemented

### Memory ingestion trust boundary
- Secret-like assignments are rejected across `key=value`, `key: value`, quoted JSON/Python-like text, nested structured objects, bearer authorization, and common credential keys.
- Normal prose such as `the password field is empty` remains allowed.
- OUTCOME / ERROR_LESSON capture passes through the same MemoryStore boundary.

### Outcome learning
- Added bounded `ExecutionOutcome` records.
- GoalRunner stores verified outcomes and deterministic failure lessons.
- Relevant OUTCOME / ERROR_LESSON entries are injected into ContextBuilder as a small `[EXPERIENCE; untrusted advice]` channel.
- Experience remains advisory and cannot grant permissions.

### Uncertainty intelligence
- Added deterministic `UncertaintyEngine` with low/medium/high/critical levels.
- Signals include ambiguity, stale references, failed verification, repeated strategy failure, replans, provider instability, weak evidence, irreversibility and risk.
- GoalRunner records uncertainty and recovery metadata on failures.

### Recovery intelligence
- Added bounded `RecoveryPolicy` for stale UI, ambiguity, failed verification, provider failures, external state changes, uncertain actions and anti-spin.
- Repeated equivalent failures force a changed recovery path rather than indefinite retry.

### Conditional deliberation foundation
- Added a bounded proposer / critic / evidence-verifier engine.
- Maximum calls and elapsed time are hard-bounded.
- It is optional and only invoked from GoalRunner when injected and trigger conditions are met.
- It cannot execute tools, grant permissions, or bypass RiskEngine/confirmation policy.

### Application interaction memory
- Added a bounded SQLite `ApplicationMemory` for successful/failing interaction patterns.
- Identity is based on application descriptors rather than transient HWND.
- Failures reduce confidence; stored geometry is a hint, never authority.

### Perception Fusion V2 metadata
- Grounding results now expose structured source types, confidence, ambiguity, evidence and observation identity.
- UIA remains authoritative when uniquely resolved.
- Vision only disambiguates existing UIA candidates in the current production path, avoiding blind coordinate clicks.

### Risk Engine V2
- Added critical external-effect escalation and high-risk-under-high-uncertainty escalation.
- Static capability risk can only be raised, never lowered.

## Validation executed

Environment note: repository imports normally require `ollama`; a temporary external test stub was used only to allow deterministic tests to collect. It is not included in the repository.

- New intelligence/perception/risk campaign: `31 passed`.
- Autonomy + memory + durability + perception + risk + intelligence campaign: `68 passed`.
- Computer/missions/planner/model-usage/durability/transactions campaign: `103 passed, 1 deselected`.
- The deselected test is Windows COM-specific and `comtypes` is unavailable in this Linux environment.
- Full suite was attempted once and stopped at collection because `hypothesis` is unavailable.
- Modified Python files pass `py_compile`.

## Not claimed

- No real Windows multi-app validation was performed here.
- No real OCR engine was added or benchmarked.
- No provider-backed deliberation A/B benchmark was executed.
- No claim is made that deliberation improves quality yet.
- Full backend release suite was not reproducible in this environment due missing optional/local dependencies.

## Highest-value next validation on the Windows development machine

1. Run the targeted suites with the project's real `.venv` and real dependencies.
2. Validate UIA/vision on Notepad, Calculator, File Explorer and Settings.
3. Add/benchmark a local OCR adapter if needed by those failures.
4. Run a conditional-deliberation A/B hard set with authoritative token/cost metrics.
5. Run the full backend suite once after Windows validations are green.
