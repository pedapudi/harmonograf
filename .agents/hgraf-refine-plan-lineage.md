---
name: hgraf-refine-plan-lineage
description: Understand revision_index, supersession, drift throttling, and terminal-status preservation when refining plans; how to test refine chains end-to-end.
---

# hgraf-refine-plan-lineage

## When to use

You are touching the refine path — the code that rewrites a live `Plan` in response to drift. This is one of the most intricate subsystems in harmonograf because every refine must preserve history, increment lineage, re-canonicalize assignees, and keep the Gantt's plan-revision banner coherent.

## Prerequisites

1. Read the proto fields that encode lineage: `proto/harmonograf/v1/types.proto:258-287`:
   - `TaskPlan.revision_reason` — free-text human summary (set at `adk.py:3483`).
   - `TaskPlan.revision_kind` — structured drift kind (`tool_error`, `new_work_discovered`, `wrong_agent`, …) used by the frontend for icons/filters.
   - `TaskPlan.revision_severity` — `"info" | "warning" | "critical"`.
   - `TaskPlan.revision_index` — monotonic counter within a lineage, 0 for initial, +1 each refine.
2. Read `client/harmonograf_client/adk.py :: _apply_refined_plan` at `adk.py:3461-3540` — the canonical swap-in path.
3. Read `client/harmonograf_client/adk.py :: refine_plan_on_drift` around `adk.py:3283-3430` — the throttle, the drift-kind dispatch, the call into `_apply_refined_plan`.
4. Read `_DRIFT_REFINE_THROTTLE_SECONDS` at `adk.py:378` — currently 2.0s per (session, drift kind).
5. Read the frontend diff computation: `frontend/src/gantt/index.ts :: computePlanDiff`. This is the code the UI uses to render "added / removed / changed" in the revision banner.

## Conceptual model

A **lineage** is a sequence of `TaskPlan` snapshots that share the same `plan_id`. Each snapshot has a `revision_index`:

- `revision_index = 0` — initial plan from `PlannerHelper.generate()`.
- `revision_index = N` — Nth refine.

The **server stores every revision** (see `server/harmonograf_server/storage/sqlite.py:121-149 task_plans` table). The `revision_index` column means we can query historical revisions for a given plan and rebuild lineage offline.

The **client only holds the latest revision** in `PlanState.plan`; the server fans out the wire `TaskPlan` message on each upsert.

**Supersession**: when `refine_plan_on_drift` decides to replace the plan, the old `PlanState` instance is logged as "superseded stale PlanState" (`adk.py:1822`). A fresh instance carries the same `plan_id` but a new `revision_index`. The wire message is a full `TaskPlan`, not a delta — the server treats it as upsert-by-id.

## Step-by-step recipes

### Adding a new drift kind that refines

1. Add the drift kind to `DriftReason` enum in `adk.py`. Grep `class DriftReason` to find it.
2. Add a throttle entry in `_DRIFT_REFINE_THROTTLE_SECONDS` or leave the default 2.0s.
3. Decide severity: `"info"` / `"warning"` / `"critical"`. Critical bypasses the throttle (see `refine_plan_on_drift`).
4. Emit the drift from wherever the new drift is detected (tool callback, after-model-callback, walker) via `self.refine_plan_on_drift(hsession_id, DriftReason(kind=..., severity=..., reason_text=...))`.
5. The rest is automatic: `_apply_refined_plan` stamps `revision_reason`, `revision_kind`, `revision_severity`, bumps `revision_index`, preserves terminal statuses, and publishes the upsert.

See the companion skill `hgraf-add-drift-kind.md` in batch 1 for the full DriftReason plumbing.

### Changing how terminal statuses are preserved

`_apply_refined_plan` reads `plan_state.tasks` and carries `RUNNING/COMPLETED/FAILED/CANCELLED` statuses forward into the new plan regardless of what the LLM returned. Code at `adk.py:3494-3520`.

- **Do not remove this preservation.** A refine that forgets a COMPLETED task is a regression the UI cannot heal — the Gantt loses the historical bar.
- The set of preserved statuses is `_TERMINAL_TASK_STATUSES` (grep for it). `RUNNING` is also preserved so the frontend doesn't flash-back to PENDING when the refine arrives mid-execution.
- If you need a status that should NOT be preserved (e.g. a new "PAUSED"), add it to the mutable set explicitly.

### Testing refine lineage end-to-end

The reference tests are in `client/tests/test_dynamic_plans_real_adk.py` using scripted `FakeLlm`. Minimal pattern:

```python
# 1. Arrange: fake LLM with two plan responses
fake = FakeLlm()
fake.responses = [plan_v0_response, plan_v1_response]

# 2. Generate initial plan (revision_index=0)
ps = planner_helper.generate(...)
assert ps.revision_index == 0

# 3. Fire a drift
_adk_state.refine_plan_on_drift(hsession_id, DriftReason(kind="tool_error", ...))

# 4. Assert lineage bumped
ps2 = _adk_state.get_plan_state(hsession_id)
assert ps2.plan.revision_index == 1
assert ps2.plan.revision_kind == "tool_error"
assert all(t.id in ps2.tasks for t in ps.tasks if t.status == "COMPLETED")  # preservation
```

For multi-step refine chains, enqueue more responses and fire drifts in sequence. The assertion is always: `revision_index` is strictly monotonic, terminal statuses preserved, `plan_id` stable.

### Testing the throttle

Fire the same drift kind twice within 2 seconds:

```python
_adk_state.refine_plan_on_drift(hsession_id, DriftReason(kind="tool_error"))
_adk_state.refine_plan_on_drift(hsession_id, DriftReason(kind="tool_error"))
# Only one refine should have happened; revision_index bumped by 1, not 2.
```

Then wait past the throttle window (use a `now_fn` injection or monkeypatch `time.monotonic`) and fire again — this time it should refine.

### Visualizing lineage in the UI

`frontend/src/components/shell/PlanRevisionBanner.tsx` reads the current `PlanRevision` list from the session store. Each revision has `revisionIndex`, `revisionKind`, `revisionSeverity`, `revisionReason`. Clicking a revision in the banner opens a diff via `computePlanDiff(current, previous)` in `frontend/src/gantt/index.ts`.

To add a new banner field (e.g. a timestamp), follow the chain:
1. Proto field on `TaskPlan`.
2. Store column in `sqlite.py task_plans`.
3. Convert in `server/harmonograf_server/convert.py`.
4. UI type in `frontend/src/gantt/types.ts` (or wherever `PlanRevision` is defined).
5. Render in `PlanRevisionBanner.tsx`.

### Throttling retune

If a drift kind is firing too often:
- Increase the per-kind throttle seconds.
- Or upgrade the severity to `critical` to bypass throttle — be careful, that's the opposite of quieting.

If a drift kind is firing too rarely:
- Confirm it isn't being suppressed by an upstream detector that runs before `detect_drift` (e.g. a callback that already consumed the response).
- Lower the throttle.

## Verification

```bash
uv run pytest client/tests/test_dynamic_plans_real_adk.py -x -q
uv run pytest client/tests/test_drift_taxonomy.py -x -q
uv run pytest client/tests/test_adk_adapter.py -x -q -k refine
cd frontend && pnpm test -- --run PlanRevisionBanner
```

## Common pitfalls

- **Forgetting to bump `revision_index`**: a refine that leaves the index unchanged gets deduped by the server-side upsert and the UI never shows it.
- **Dropping terminal preservation**: breaks history. Never short-circuit the `preserved_statuses` loop.
- **Replacing `plan_id`**: creates a new lineage rather than refining the existing one. The UI shows a disconnected second plan — usually wrong. Always reuse the `plan_id` from the existing `PlanState`.
- **Drift firing inside the refine**: if `_apply_refined_plan` mutates state that a callback watches, you can recursively fire refine. Critical drifts bypass the throttle, so a bug here is an infinite loop. The lock at `adk.py:3493` serializes refines, but cross-session loops still happen — test with two concurrent sessions.
- **Ignoring the `revision_severity` contract**: the frontend assumes exactly `"info" | "warning" | "critical"`. An empty string (the default) means "initial plan". Any other string silently renders with the default icon — easy to miss in review.
- **Refusing to canonicalize**: `_canonicalize_plan_assignees` at `adk.py:3478` rewrites bad assignee ids to the host agent. If you skip it, the LLM's hallucinated agent ids appear in the Gantt as missing rows.
