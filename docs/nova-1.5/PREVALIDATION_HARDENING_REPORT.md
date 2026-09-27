# Nova 1.5 — Pre-validation hardening pass

This pass intentionally improves the Nova 1.5 codebase before the larger Windows/provider validation campaign. It does not modify the frozen Nova V1 evidence under `docs/v1-hardening/` and does not claim new real-world capability scores.

## Improvements implemented

### Memory safety and lifecycle

- Structured JSON strings are recursively inspected by the secret-ingestion boundary before persistence.
- Embeddings containing NaN or infinity are rejected instead of entering semantic retrieval.
- Explicit memory expiry is normalized to timezone-aware UTC and expired active memories are pruned before remember/search/list/get operations.
- Expired memories are no longer returned by `get`.
- Corrupt optional embedding/tag JSON degrades safely instead of crashing memory retrieval.
- Added an explicit bounded `touch()` path so context assembly can update usage metadata without executing duplicate searches.

### Context efficiency and trust separation

- `ContextBuilder` now performs one memory retrieval pass instead of separate general-memory and outcome/error searches.
- Outcome and `ERROR_LESSON` records are kept in the explicitly untrusted experience channel rather than being mixed with ordinary durable memory.
- Project context is explicitly labelled as untrusted repository data.
- Project/mission insertion now respects the global context budget more directly.

### Conversation longevity and recovery metadata

- Normal conversations no longer dead-end simply because the in-memory message window reached its configured retention size. Oldest messages roll out while the active/persisted history remains bounded.
- A durable `trimmed_message_count` records how much history has rolled out.
- Terminal/cancelled goals and missions are removed from the conversation's active-work associations rather than accumulating forever.
- Conversation restore tolerates malformed optional metadata/messages instead of failing the whole service startup.
- Conversation deletion explicitly removes message rows even for databases created before foreign-key enforcement was enabled.

### Recovery intelligence correctness

- Runtime error categories are normalized to the recovery taxonomy. Real categories such as `STALE_ELEMENT_REFERENCE`, `AMBIGUOUS_ELEMENT_REFERENCE`, `provider_unavailable`, `PERCEPTION_TARGET_NOT_FOUND`, and `interrupted_uncertain` now map to the intended recovery strategy.
- Repeated-strategy detection now tracks failures per action fingerprint instead of incorrectly using the total failed-step count for the whole goal.
- This avoids unrelated failures falsely triggering anti-spin behavior.

### Conditional deliberation hardening

- Deliberation prompts now clearly mark objective/evidence/candidate material as untrusted data.
- The engine accepts bounded structured fields when providers return them, while remaining backwards compatible with prose `content`/`summary` responses.
- `alternative`, `rejected_options`, `evidence_used`, `require_human`, and `require_more_observation` can now be preserved structurally instead of being discarded.
- Deliberation remains advisory and cannot grant tool authority.

### Perception/application memory robustness

- Visual image cache operations are guarded by a re-entrant lock and use incremental byte accounting during eviction.
- Resolved image references are moved to the MRU end of the cache.
- Visual grounding prompt labels request/candidate text as untrusted UI data.
- Empty application identity no longer collapses unrelated unknown applications into the same memory bucket.
- Application-memory writes are synchronized, corrupt geometry metadata degrades safely, old hints receive a recency penalty, and the interaction store is bounded.

### Risk intelligence

- Secret-like UI input uses the same structured secret detector as MemoryStore, so JSON-shaped credentials also raise contextual risk.

### Observability and SQLite runtime

- Fixed a duplicate accounting defect where `ModelUsageStore.record_failure()` could write two failure records when no provider attempt history existed.
- Added successful-attempt authoritative-usage coverage alongside the original all-attempt metric.
- Model-usage and application-interaction stores are bounded to avoid unbounded growth.
- Event journal now has correlation indexes for goal, conversation, mission, and action queries, plus `for_mission()` retrieval.
- Corrupt optional structural JSON in an event no longer crashes journal reads.
- Core runtime stores touched by this pass use SQLite WAL, normal synchronous mode, and a bounded busy timeout for better local read/write concurrency.

## Files changed

- `nova_api/application_memory.py`
- `nova_api/autonomy.py`
- `nova_api/context_builder.py`
- `nova_api/conversation_service.py`
- `nova_api/conversation_store.py`
- `nova_api/deliberation.py`
- `nova_api/journal.py`
- `nova_api/memory_store.py`
- `nova_api/model_usage.py`
- `nova_api/recovery.py`
- `nova_api/risk.py`
- `nova_api/visual.py`
- `tests/test_prevalidation_hardening_v15.py` (new targeted regression coverage)

## Validation performed during the edit pass

This was deliberately not the final validation campaign.

- All Python sources compiled successfully with `compileall`.
- New focused hardening regressions: **6 passed**.
- Combined smoke campaign around memory, context, usage, risk, intelligence, autonomy, conversations, perception, visual and computer modules: **165 passed** before one environment-only failure.
- The only failure in that smoke campaign requires importing Windows `comtypes`, which is unavailable in this Linux environment. It is not classified as a product regression.
- A temporary external `ollama` import stub was used only to let deterministic test modules collect; it is not included in this repository/package.

## Intentionally deferred

The following still require the user's real Windows/provider environment and belong to the separate validation phase:

- multi-application Windows UIA/OCR/vision run,
- several-hour wall-clock autonomy,
- provider-backed deliberation A/B quality measurement,
- authoritative cross-provider token/cost coverage,
- full repository test suite with all project dependencies installed.

No commit or tag was created.
