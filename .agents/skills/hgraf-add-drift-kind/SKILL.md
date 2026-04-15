---
name: hgraf-add-drift-kind
description: End-to-end checklist for introducing a new drift kind across client detection, throttling, frontend metadata, banner rendering, and regression tests.
---

# hgraf-add-drift-kind

## When to use

You are expanding the drift taxonomy that drives dynamic replanning. A new drift kind is warranted when there is a distinct *cause* the planner should see in its `refine()` prompt, distinct from all existing kinds in `client/harmonograf_client/adk.py:352-368`. Examples of valid new kinds: "tool took longer than predicted duration", "agent returned a disallowed mime type", "LLM emitted malformed function-call args that fell back to text". Examples of things that are **not** a new drift kind: a slight variation of an existing one (add a `detail` field instead), or a UI-only badge (add it in `driftKinds.ts` without touching the client).

## Prerequisites

1. Read the existing taxonomy end to end:
   - `client/harmonograf_client/adk.py:326-378` — `DriftReason` dataclass + all `DRIFT_KIND_*` constants + throttling constants.
   - `frontend/src/gantt/driftKinds.ts:30-58` — `DRIFT_KIND_META` record (category, icon, severity, human label).
   - `client/tests/test_drift_taxonomy.py` — the duck-typed unit tests that cover every kind.
2. Skim [`docs/design/10-frontend-architecture.md`](../../../docs/design/10-frontend-architecture.md) and `AGENTS.md` → *Plan execution protocol* section to confirm the new kind fits the "callback-driven drift → refine → computePlanDiff → banner" pipeline.

## Step-by-step

### 1. Client: declare the kind constant

Edit `client/harmonograf_client/adk.py`. Add a new constant alongside the existing `DRIFT_KIND_*` block (around line 352-368). Use the same naming shape — snake-case, no prefix, stable for wire compatibility:

```python
DRIFT_KIND_TASK_DURATION_EXCEEDED = "task_duration_exceeded"
```

Decide **severity** (info | warn | error) and **recoverable** (True for most — only False when the planner cannot meaningfully refine, like hard tool failures). These live in the `DriftReason` instance you construct at the detection site, not on the constant.

### 2. Client: add the detection path

There are three detection paths. Pick the one that matches the signal's origin:

- **Inside `detect_drift()`** (`adk.py:2917-3050`) — for drifts derived from span/event scanning. Append a new branch that appends `DriftReason(kind=DRIFT_KIND_..., detail=..., severity=..., recoverable=...)` to the returned list.
- **Inside `after_model_callback()`** (`adk.py:1227-1276`) — for drifts the LLM expressed in prose (refusal, merge, split, reorder patterns).
- **Inside `before_tool_callback()`** (`adk.py:1299-1316`) or a reporting-tool interceptor — for drifts raised by the agent via a reporting tool (e.g. `report_task_failed` → `task_failed_*`, `report_plan_divergence` → `plan_divergence`).

**Critical:** route every drift through the same exit point — the function that eventually calls `refine_plan_on_drift()` (`adk.py:3256-3350`). Do not call `planner.refine()` directly from your detection code; you will miss throttling and unrecoverable cascade logic.

### 3. Client: respect throttling

`adk.py:373-378` defines:
```python
_STAMP_MISMATCH_THRESHOLD = 3
_DRIFT_REFINE_THROTTLE_SECONDS = 2.0
```

The refine loop coalesces repeated drifts within `_DRIFT_REFINE_THROTTLE_SECONDS`. If your new drift is noisy (fires many times per turn — e.g. repeated `failed_span` on retries), follow the `multiple_stamp_mismatches` pattern: count occurrences, only promote to a `DriftReason` once a threshold is crossed. Do **not** lower the global throttle; add a kind-local threshold constant next to `_STAMP_MISMATCH_THRESHOLD`.

### 4. Client: unit test in test_drift_taxonomy

Add a test case to `client/tests/test_drift_taxonomy.py` following the existing pattern (duck-typed fakes; no real ADK import). The fixtures at the top of the file (`FakeClient`, `FakeFunctionCall`, `RecordingPlanner`, roughly lines 41-80) are reusable — construct whatever event sequence triggers your new kind, feed it through `detect_drift` (or the relevant callback), and assert:
- The returned list contains exactly one `DriftReason` with your new `kind` string.
- `severity` and `recoverable` match your design.
- `detail` includes the span/event id that triggered it.

Run:
```bash
cd client && uv run --with pytest --with pytest-asyncio python -m pytest tests/test_drift_taxonomy.py -q
```

### 5. Client: end-to-end test in test_llm_agency_scenarios

If the drift is LLM-agency-driven (callback path), add a second test to `client/tests/test_llm_agency_scenarios.py`. This file imports real `google.adk.Event` and `google.genai.types` payloads (see the skip guard at lines 29-37) and exercises `detect_drift → refine_plan_on_drift` through the real seam. Use the existing agency scenarios as templates.

### 6. Frontend: drift kind metadata

Edit `frontend/src/gantt/driftKinds.ts:30-58`. Add an entry to the `DRIFT_KIND_META` record keyed by your `"task_duration_exceeded"` string (must match the client constant value exactly — this is the wire key):

```ts
task_duration_exceeded: {
  label: "Task overran predicted duration",
  category: "schedule",         // or: tool | plan | agency | user | system
  icon: "clock-alert",          // MDI icon name, matches existing convention
  severity: "warn",             // info | warn | error
},
```

`parseRevisionReason()` at `driftKinds.ts:60-87` parses `"{kind}: {detail}"` format from `TaskPlan.revisionReason`, and `getDriftKindMeta()` at lines 89-92 does the lookup. Both will pick up your new entry automatically — no wiring required.

### 7. Frontend: verify banner preview

The `PlanRevisionBanner` component (`frontend/src/components/shell/PlanRevisionBanner.tsx`) reads from `TaskRegistry` revisions and renders using `getDriftKindMeta()`. After step 6, run:
```bash
cd frontend && pnpm lint && pnpm build
```
Then `make demo` and drive a scenario that actually emits the new drift. Visually confirm:
- Banner shows your icon + label.
- Banner category colour matches the rest of your category (see `driftKinds.ts` category → colour).
- The diff drawer shows the added/removed/modified tasks that the planner returned in its `refine()` response.

### 8. Plan diff integration (usually no-op)

`computePlanDiff()` at `frontend/src/gantt/index.ts:129-181` is drift-kind-agnostic — it diffs task sets and edge sets. You do **not** need to touch it unless your drift kind changes the diff semantics (e.g. a "renamed task" drift that should match by semantic id not task id). That is a much larger change — open a design doc first.

## Verification

```bash
# 1. client unit tests
cd client && uv run --with pytest --with pytest-asyncio python -m pytest tests/test_drift_taxonomy.py tests/test_llm_agency_scenarios.py tests/test_invariants.py -q

# 2. full client suite
cd client && uv run --with pytest --with pytest-asyncio python -m pytest -q

# 3. frontend type + lint
cd frontend && pnpm lint && pnpm build

# 4. live smoke
make demo
# Drive a scenario that triggers the new drift; confirm banner renders.
```

## Common pitfalls

- **Wire-key drift.** The client constant value (`"task_duration_exceeded"`) is the wire key. Changing it later breaks any persisted sqlite TaskPlan rows and any frontend that cached `DRIFT_KIND_META`. Pick a good name once.
- **Forgetting throttling.** A new drift kind that fires on every span end will hammer the planner. `_DRIFT_REFINE_THROTTLE_SECONDS = 2.0` gives you coarse global protection but you still want per-kind thresholds for noisy signals.
- **Routing around `refine_plan_on_drift`.** The function at `adk.py:3256-3350` applies the unrecoverable-cascade logic — if your drift is `recoverable=False` and you bypass it, child tasks stay RUNNING forever and the invariant checker (`client/harmonograf_client/invariants.py:78-100`) will complain on the next sweep.
- **Skipping `driftKinds.ts`.** If you add the client constant but forget the frontend entry, `getDriftKindMeta()` returns `UNKNOWN_DRIFT_KIND_META` and the banner shows a generic icon. Lint will not catch this — it is a string lookup.
- **Mutating state from detection code.** Detection returns `DriftReason` objects. Do not flip task status in `detect_drift()`. All state transitions go through `_set_task_status()` at `adk.py:242-280` — see `hgraf-safely-modify-adk-py`.
- **Duplicate-constant naming collision.** If you pick a name already used in `DRIFT_KIND_META` (frontend) or `DRIFT_KIND_*` (client) but with different semantics, the type system will not catch it — the strings will silently merge. Grep both files first.
