# Nova 1.5 — Ultimate pre-validation hardening

This pass consolidates the highest-value findings from the independent pre-validation audits without performing a broad architectural rewrite.

## Implemented

- **Re-observe before mutate for UIA**: mutating UI actions re-resolve the opaque element immediately before execution and require the stable semantic target fingerprint to remain unchanged. A changed/stale/ambiguous target fails closed as `TARGET_FINGERPRINT_CHANGED`. UI inspection now exposes only the safe semantic fingerprint, never native handles.
- **Recovery mapping for changed UI state**: `TARGET_FINGERPRINT_CHANGED` normalizes to `STATE_CHANGED_EXTERNALLY`, which selects re-observation rather than blind retry.
- **Mission context roll-up**: active mission context remains deterministic and bounded while now carrying compact completed-step count, last safe result metadata, and last error. Full UI trees, file bodies and diffs are not injected.
- **Verified rollback semantics**: transaction public metadata explicitly distinguishes verified rollback. Goal recovery no longer silently ignores rollback conflicts; a failed rollback is journaled and blocks the goal instead of replanning over uncertain state.
- **Global provider backoff hardening**: provider health is process-global and thread-safe. Retryable repeated failures use bounded exponential backoff shared across Nova call sites and reset only after a successful provider call.
- **End-to-end cancellation honesty**: running goals now treat cancel as a request observed by the runner. Conversation cancellation journals `generation.cancel_requested` and no longer immediately claims terminal cancellation before the underlying engine/tool reaches a safe boundary. If cancellation arrives while a tool is in flight, Nova attempts verified rollback when possible and blocks for review when a successful non-reversible mutation may already have occurred.
- **Persistence schema guard**: GoalStore, MissionStore, TransactionStore and EventJournal now use SQLite `user_version` as a forward-compatibility guard. Pre-versioned databases are adopted as schema v1; databases from an unknown newer schema fail closed rather than being interpreted silently.
- **State authority decision for this milestone**: no event-sourcing rewrite was introduced. Durable stores remain authoritative for resumable state; EventJournal remains the bounded audit/evidence stream. Existing restart reconciliation is preserved. A full event-sourcing conversion is explicitly deferred until after real validation because it would materially change the blast radius immediately before measurement.

## Validation executed

Targeted neighboring suites:

```text
117 passed, 3 deselected
```

The three deselections are environment-specific tests already known to depend on unavailable optional Windows/provider packages in this Linux environment. No full-suite or multi-hour Windows validation was run in this pass.

All modified Python modules pass `py_compile`.

Frozen V1 evidence check:

```text
20 / 20 files under docs/v1-hardening unchanged by SHA-256
```

## Explicitly deferred until real validation

- Full event-sourcing conversion of Goal/Mission/Journal/Transaction state.
- GoalRunner / MissionManager / AgentLoop merger.
- Project Brain graph/indexer redesign.
- Heavy sandbox redesign.
- Large frontend/SSE rewrite.
- Long-running semantic summarization driven by extra model calls. Current context remains hard-bounded and deterministic; real endurance metrics should determine whether a model-generated hierarchical mission summary is worth its token and correctness cost.

## Remaining real-world unknowns

- Windows COM/UIA races at OS timing boundaries cannot be proven in this Linux environment.
- Cancellation cannot preempt an arbitrary blocking native/API call in the middle of that call; it is observed at the next controlled boundary. The new logic reports this honestly and protects reversible transactions where possible.
- Exponential provider health is process-global, not cross-process distributed state.
- Schema guards detect a newer incompatible database but do not yet provide migrations beyond v1.
