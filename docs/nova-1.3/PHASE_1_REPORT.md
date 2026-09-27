# Nova 1.3 — Perception Fusion Foundation

## Scope

This phase completes the next Nova 1.2 measurement/risk foundation and adds a bounded Nova 1.3 perception-fusion primitive without changing the frozen V1 benchmark artifacts.

## Nova 1.2 completion work

### Memory V2 benchmark harness

Added `nova_api/memory_benchmark.py` with deterministic top-1/top-k recall evaluation helpers. The harness is provider-neutral and can be reused with the current lexical store or any injected embedding backend.

No claim is made here that the deterministic test embedder represents production semantic quality. A real embedding-provider recall campaign remains a separate empirical benchmark.

### Context-aware risk

The Risk Engine can now consume bounded target context in addition to static capability metadata and arguments. Risk can be raised for:

- protected/password targets;
- sensitive applications such as terminals, credential/security tools and installers;
- sensitive window contexts;
- high-impact target names such as payment, deletion, deployment or permission actions;
- stale/ambiguous target context.

The engine remains monotonic: contextual logic may raise risk but never lower the capability's static baseline.

`ComputerController.risk_context()` exposes only bounded semantic metadata. It never exposes native handles, raw UI values or secrets. `CapabilityRegistry` carries this resolver, and `GoalRunner` recalculates contextual risk immediately before execution.

## Nova 1.3 perception fusion foundation

Added the read-only `computer.perception.ground` capability.

Flow:

1. inspect the target window through UIA;
2. score visible/enabled semantic candidates deterministically;
3. if a unique candidate is strong enough, return its existing opaque `element_ref` without invoking vision;
4. if UIA is insufficient and a window-scoped image is supplied, invoke vision only to arbitrate among a bounded shortlist of UIA candidates;
5. reject cross-window/display image grounding;
6. return an existing UIA `element_ref` only, never pixel coordinates or a click instruction.

This is intentionally UIA-first and vision-fallback. It does not perform actions itself.

## Additional portability fix

`Workspace.resolve()` now rejects Windows drive/root path syntax even when validation happens on a POSIX CI host. This fixed an existing cross-platform planner test where `C:/outside/...` was incorrectly interpreted as a relative POSIX path.

## Validation in the current environment

Successful targeted runs:

- Memory/Risk/Perception/Visual/Computer: `67 passed, 1 deselected`.
  - The deselected test requires `comtypes`, unavailable on this Linux environment.
- Autonomy/Planner/Execution Kernel/Endurance: `78 passed`.
- Windows path-system regression tests that are platform-independent: `34 passed`.
- Modified Python modules: `py_compile` PASS.

Environment-only collection blockers observed outside those successful runs:

- `ollama` Python package absent. A temporary external stub was used only to allow unrelated modules to import during local targeted validation; the repository was not modified with that stub.
- `comtypes` absent on Linux.
- `hypothesis` absent for one broader legacy property-test module.

No full-suite result is claimed from this environment.

## Frozen baseline integrity

Files under `docs/v1-hardening/` were byte-compared with the input Nova 1.2 archive and were unchanged.

## Known limitations / next empirical work

- Production semantic-memory quality still needs a real recall benchmark using the selected local/cloud embedding backend.
- Fusion currently grounds only to UIA candidates already present in the accessibility tree. Pure-canvas targets remain future work.
- No local OCR engine has been introduced yet.
- No multi-application Windows real-world matrix has been run in this environment.
- Vision arbitration depends on a vision-capable provider when deterministic UIA grounding is ambiguous.
