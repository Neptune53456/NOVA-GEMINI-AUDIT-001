# Conditional Deliberation A/B Plan

Compare the same difficult scenarios under:

- A: normal single-model path
- B: conditional deliberation only when deterministic uncertainty reaches HIGH/CRITICAL or explicit verification/risk triggers fire

Measure `completed_verified`, critical failures, false verified success, replans, model calls, authoritative tokens where available, latency, fallbacks and rollbacks.

Do not promote deliberation as beneficial unless B improves verified outcomes without a safety regression and with an explicit cost/latency tradeoff.
