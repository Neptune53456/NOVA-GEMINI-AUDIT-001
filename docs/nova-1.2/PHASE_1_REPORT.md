# Nova 1.2 Phase 1 — Memory V2 + Risk Intelligence

## Scope

This phase extends the Nova 1.1 Phase 2 baseline without changing frozen V1 benchmark artifacts.

### Memory V2

- Keeps the deterministic lexical/rule memory path as the default and audit-friendly baseline.
- Adds an optional semantic embedding backend to `MemoryStore`.
- Stores bounded vectors separately in SQLite (`memory_embeddings`) rather than replacing lexical indexes.
- Blends semantic similarity with existing importance, confidence, provenance, taxonomy and lexical scoring.
- Keeps unknown/unavailable embedding behavior fail-soft: semantic retrieval simply disables itself rather than blocking memory.
- Adds a small experience-history signal for previously useful `OUTCOME`/`ERROR_LESSON` items.
- `ContextBuilder` may retrieve memory for ordinary requests when a semantic backend is explicitly configured; without one, V1/V1.1 retrieval gating is preserved.

The repository does **not** force a cloud embedding provider. The semantic backend is injected, so a local or provider-backed embedder can be selected later without coupling durable memory to one vendor.

### Risk Intelligence

- Adds deterministic `RiskEngine` / `RiskAssessment`.
- Static capability risk remains the floor and can never be reduced by the dynamic engine.
- Desktop state-changing UI operations are elevated to medium risk for traceability without confirmation spam.
- Secret-like text entry is elevated to high risk and requires confirmation.
- Sensitive filesystem targets are elevated to high risk and require confirmation.
- Irreversible non-visual desktop effects are high risk and require confirmation.
- Goal plans persist dynamic `risk` plus bounded machine-readable `risk_reasons`.
- GoalRunner evaluates risk again immediately before execution, so an old plan cannot bypass a stronger current policy.

## Validation

New targeted tests:

- `tests/test_memory_v12.py`
- `tests/test_risk_v12.py`

Result in the ChatGPT container:

- 6/6 new Nova 1.2 tests passed.
- 24 neighboring autonomy/execution tests passed.
- 2 neighboring FastAPI tests could not collect their real planner because the container lacks the optional `ollama` Python dependency. This is an environment limitation already seen in V1.1 validation, not a Nova 1.2 product failure.
- Modified Python modules compile successfully with `py_compile`.

## Known limits / deferred work

- No production embedding model is selected in this phase. That choice should be benchmarked for recall, privacy, latency and footprint before becoming a default.
- Semantic vector migration/backfill occurs when items are written with an embedder configured; an explicit offline backfill utility can be added once a production embedder is chosen.
- The Risk Engine currently reasons from capability metadata and sanitized arguments. Opaque UI references intentionally do not reveal application identity, so application-specific risk needs a future trusted target-context resolver rather than leaking raw desktop identifiers to the model.
- This phase does not add the Deliberation Engine.
- No commit or tag was created.
