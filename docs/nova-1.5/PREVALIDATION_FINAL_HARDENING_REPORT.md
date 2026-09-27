# Nova 1.5 Pre-Validation Final Hardening

Date: 2026-09-19

This pass deliberately improves the repository before the larger Windows/full-suite validation campaign. It does not claim multi-hour or multi-app real-world validation.

## Main improvements

### Memory performance and resilience
- Removed the semantic-search N+1 SQLite lookup pattern by loading candidate embeddings in one bounded query.
- Equal-score memories now prefer newer durable evidence rather than older rows.
- Legacy/corrupted `tags_json` no longer prevents MemoryStore startup or term-index repair.

### Mission durability and context relevance
- MissionStore now uses WAL, busy timeout and NORMAL synchronous mode.
- Added indexes for conversation and state/update-time lookups.
- Added bounded filtering by `conversation_id` and mission states instead of forcing ContextBuilder to load/filter the full recent set.
- Corrupted mission JSON fails closed on direct access while a damaged row no longer poisons mission listing/context retrieval.
- Mission inserts now name columns explicitly, reducing schema-migration fragility.
- Missing-row updates now raise `MissionNotFound` rather than pretending persistence succeeded.
- ContextBuilder gives active same-conversation mission state priority over broad ProjectBrain context and does not inject old completed/cancelled work into ordinary requests.

### Goal persistence resilience
- GoalStore now uses WAL, busy timeout and NORMAL synchronous mode.
- Corrupted legacy goal rows no longer prevent Nova startup.
- Direct access to a corrupted goal fails closed with `corrupt_goal_state`; list operations skip only the damaged row.

### Provider degradation
- `model_router.py` no longer fails to import when the optional Python Ollama client is absent.
- Local chat/embed calls fail explicitly as `local_model_unavailable`, while remote-provider routing remains importable/usable.

### Model-usage efficiency
- Provider usage normalization avoids duplicate conversion work.
- Multi-attempt/fallback usage records are inserted in one SQLite transaction instead of one connection/transaction per attempt.
- Added a goal/timestamp index for usage-history reads.

### SQLite concurrency consistency
WAL/busy-timeout/NORMAL synchronous handling was extended to:
- application interaction memory
- durable confirmation state
- ProjectBrain
- file transactions
- GoalStore
- MissionStore

This aligns these frequently used stores with the journal/memory/conversation stores and reduces unnecessary writer contention during long-running execution.

## Regression coverage added

`tests/test_final_prevalidation_v15.py` covers:
- MissionStore conversation/state filtering.
- Fail-safe behavior for corrupted mission rows.
- Active mission priority in ContextBuilder.
- Memory recency tie-breaking.
- Graceful absence of the Ollama client.
- Fail-safe behavior for corrupted goal rows.
- MemoryStore restart with corrupted legacy tags.

## Validation actually executed

Targeted campaign:

```text
68 passed in 2.00s
```

Covered suites:
- `test_final_prevalidation_v15.py`
- `test_memory_v12.py`
- `test_missions.py`
- `test_mission_durability_v11.py`
- `test_autonomy.py`
- `test_durable_autonomy_v11.py`
- `test_model_usage.py`
- `test_prevalidation_hardening_v15.py`
- `test_real_world_v15.py`
- `test_transactions_durable.py`

`py_compile` also passed for every Python file modified in this pass.

The frozen V1 evidence under `docs/v1-hardening/` was hash-compared against the input archive and is unchanged (20 files).

## Deliberately deferred

The following remain for the dedicated validation phase:
- full backend suite
- Windows COM/UIA validation
- browser/Explorer/Settings multi-app validation
- multi-hour endurance/chaos tests
- real-provider A/B deliberation benchmark
- production semantic-embedding quality benchmark

No commit or tag was created.
