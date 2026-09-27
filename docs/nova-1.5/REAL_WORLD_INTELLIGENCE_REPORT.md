# Nova 1.5 — Real-World Intelligence Foundation

## Scope

This milestone continues from the Nova 1.4 Intelligence Foundation and focuses on practical desktop perception, measurable deliberation, durable application interaction memory, and longer-running local execution validation. Frozen Nova V1 artifacts under `docs/v1-hardening/` were not modified.

## Implemented

### Perception fusion

- Added a local screenshot OCR boundary in `nova_api/local_ocr.py` using the local `tesseract` executable when available.
- Added the `computer.visual.ocr` observation-only capability.
- `computer.perception.ground` now follows a bounded order:
  1. semantic UIA,
  2. prior successful application-interaction hints,
  3. automatic window capture when deterministic grounding is insufficient,
  4. local OCR disambiguation,
  5. vision disambiguation of an existing UIA shortlist,
  6. fail closed.
- OCR or vision never turns an arbitrary coordinate into an executable target. Grounding still returns an existing opaque UIA `element_ref`.
- Perception results expose safe structural evidence including source types, confidence, ambiguity, OCR/vision usage and a structural fingerprint.
- UI inspection now exposes safe structural metadata (`automation_id`, structural path and bounds) without native COM objects/handles.

### Application interaction memory

- Existing `ApplicationMemory` was upgraded with bounded normalized intent text and lexical similarity fallback.
- Related intents can reuse a previous structural hint instead of requiring exact-string identity.
- Repeated failures continue to reduce confidence faster than successes increase it.
- `GoalRunner` now feeds verified/failed UI interactions into application memory when safe structural context is available.
- Historical hints are advisory only and cannot authorize actions.

### Deliberation reliability

- Deliberation model/provider failures now degrade safely instead of crashing the goal runner.
- Failed calls are counted and reported.
- Deliberation elapsed time is measured.
- GoalRunner now counts deliberation calls against the goal model-call budget.
- If the model-call budget is exhausted, conditional deliberation is skipped structurally instead of silently exceeding the budget.

### Observability

- Perception and OCR capability events now record only bounded structural metadata in the Event Journal.
- Raw screenshot bytes, query text and OCR content are not copied into the journal.
- Added `nova_api/intelligence_benchmark.py` for comparable single-model vs conditional-deliberation measurements without inventing unavailable token totals.

### Validation harnesses

- Added `scripts/windows_multiapp_validation.py`: read-only UIA validation for visible Notepad, Calculator, File Explorer, Settings and browser windows. It never clicks, types, changes settings or closes user windows.
- Added `scripts/nova_v15_endurance.py`: deterministic restart/endurance probe using read-only local goals and no provider calls.

## Additional bug fixed

While adding safe structural UI metadata, a loop-local descriptor reuse defect was exposed in `ComputerController.inspect_ui`: public metadata could reuse the final element descriptor for multiple elements. The descriptor is now recomputed for each element before public serialization.

## Validation executed in this environment

### Targeted tests

Final focused campaign:

- `77 passed` for perception, visual, Nova 1.5, intelligence and autonomy suites.
- A wider relevant campaign produced `138 passed, 2 deselected` when excluding two independently classified non-product blockers:
  - Windows-only `comtypes` is unavailable in this Linux validation environment.
  - one legacy test expects the removed private in-memory `TransactionStore._items` field; the same test also fails on the supplied Nova 1.4 baseline, so this is not a Nova 1.5 regression.

### Local OCR real execution

The installed local Tesseract 5.5.0 backend was exercised against a generated PNG containing `NOVA SAVE REPORT`.

Observed OCR spans included `NOVA`, `SAVE` and `REPORT`, each with approximately 0.96 reported confidence.

This proves the local OCR adapter executes in this environment. It does not prove Windows screenshot OCR quality across real applications.

### Endurance probe

`scripts/nova_v15_endurance.py --iterations 120`:

- status: PASS
- 120/120 goals `completed_verified`
- 120 unique goal IDs
- 0 provider calls
- elapsed: 628 ms in this environment
- median step latency: 3 ms
- max step latency: 9 ms

This is a deterministic restart/store endurance probe, not a claim of multi-hour real desktop autonomy.

### Windows multi-application validation

Not executable here because the validation environment is Linux. The harness exits explicitly with `NOT_EXECUTABLE: windows_required`.

## Full-suite attempt

The full backend suite was attempted once. Collection stopped before execution because `hypothesis` is not installed in this environment. `ollama` is also absent, so targeted tests used an external temporary import stub solely to allow collection. The stub is not included in the repository/package and no provider behavior was claimed from it.

## Remaining limitations

- Real Windows multi-application UIA/OCR/vision validation still needs to run on the user's Windows machine.
- OCR currently depends on a locally installed Tesseract executable when used; absence is a normal graceful-degradation path.
- Vision remains provider-dependent and is used only after deterministic/UIA/OCR paths fail to disambiguate.
- Conditional deliberation is instrumented and safer, but its quality benefit still requires A/B testing with real providers.
- This milestone does not claim several-hour autonomous operation. The included endurance probe validates repeated durable local goals, not wall-clock duration.

## Highest-value next validation

Run the read-only Windows multi-app harness first, then a controlled A/B hard-set for conditional deliberation with authoritative model-call/token metrics. Only after those are measured should Nova 1.6 expand self-improvement autonomy.
