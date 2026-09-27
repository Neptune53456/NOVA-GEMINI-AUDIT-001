# Nova 1.1 — Phase 1 report

## Scope

Phase 1 completes the Observability V2 and Durable Autonomy foundation without modifying the frozen V1 benchmark artifacts.

## Implemented

- persistent model-usage ledger with provider/model attempt history, fallback metadata, provider-reported token normalization and privacy-safe structural records;
- provider/model summaries with attempts, successes/failures, fallback count, authoritative token coverage and latency statistics only when sample size supports them;
- durable filesystem transaction journal and bounded pre-images for rollback;
- restart reconciliation for uncertain filesystem writes without blind replay;
- durable confirmation records bound to exact goal/mission step, capability, target/effect fingerprint and expiry while raw HMAC tokens remain process-local;
- bounded SQLite conversation store restoring recent messages and goal/mission associations while never reviving a generation as busy after restart.

## Security semantics

A restart never revives a raw confirmation token. Pending confirmation records from another process generation expire, and the current step receives a fresh confirmation request. Changed effects invalidate approval.

Unknown provider token counts remain `null`; estimates are not promoted to authoritative usage.

## Known limits

- cost estimation remains `null` until a trustworthy pricing contract exists;
- conversation durability is intentionally bounded to the existing service message limit, not an unbounded transcript archive;
- only reversible workspace filesystem writes have deterministic persistent rollback/reconciliation today;
- Windows UIA and real provider validation must still be executed on the user's Windows environment.
