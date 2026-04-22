---
name: hgraf-refine-plan-lineage
description: Understand revision_index and plan lineage. Plan refine logic moved to goldfive; harmonograf only stores + renders the revisions.
---

# hgraf-refine-plan-lineage

## When to use

You're touching how harmonograf reads or renders plan-revision
history — revision banners, the trajectory view's rev chips, the
intervention aggregator's outcome attribution, or the SQLite
`task_plans.revision_*` columns.

The *refine decision logic* (when to refine, what drift kind to
attribute, how to compose the LLM prompt) is in goldfive — see
`goldfive.planner.*` and `goldfive.DefaultSteerer`. This skill
covers the harmonograf side only.

## Conceptual model

A **lineage** is a sequence of `TaskPlan` rows sharing the same
`plan_id`. Each row has a `revision_index`:

- `revision_index = 0` — initial plan (goldfive's first
  `PlanSubmitted`).
- `revision_index = N` — Nth refine (goldfive's Nth `PlanRevised`).

Four columns encode lineage:

| Column | Role |
|---|---|
| `revision_index` | Monotonic within a `plan_id`. |
| `revision_kind` | Lowercase drift kind that caused the revise (`user_steer`, `tool_error`, `looping_reasoning`, `cascade_cancel`, …). Empty for `revision_index=0`. |
| `revision_severity` | `info` / `warning` / `critical`. |
| `revision_reason` | Free-text human summary from the planner. |

See `server/harmonograf_server/storage/sqlite.py` for the SCHEMA
and the backfill `ALTER TABLE` block.

## Wire path

1. Goldfive emits `PlanSubmitted` (initial) or `PlanRevised` events.
2. The client sends them as `TelemetryUp.goldfive_event` envelopes.
3. `server/harmonograf_server/ingest.py` dispatches to
   `_handle_plan_submitted` / `_handle_plan_revised`.
4. The handler stores the new `TaskPlan` row (new `revision_index`)
   and publishes a delta on the bus.
5. Frontend's `TaskRegistry.upsert(plan)` runs `computePlanDiff`
   against the previous revision and stores a `PlanRevision`.

## Intervention aggregator interplay

`server/harmonograf_server/interventions.py` joins revisions with
drifts + annotations. Two important mechanics:

- **Attribution.** A drift that fired within `_OUTCOME_WINDOW_S`
  (5 s) before a `PlanRevised` gets the `plan_revised:rN` outcome
  label. User-control drifts use `_USER_OUTCOME_WINDOW_S` (300 s)
  because the planner's refine LLM takes tens of seconds on local
  models.
- **Dedup by annotation_id.** When a user STEER causes a revise,
  three rows land (annotation, USER_STEER drift, PlanRevised
  row). They collapse onto the annotation row via `_merge_by_annotation_id`.

## UI path

- `docs/user-guide/trajectory-view.md` describes the ribbon + rev
  chips the Trajectory view renders from the lineage.
- `frontend/src/gantt/index.ts :: computePlanDiff` computes the
  diff between successive revisions. Updates to `Task` shape must
  keep this in sync.

## Pitfalls

- **Don't rewrite history.** `revision_index` is monotonic. If you
  re-ingest an event with a lower index, the UI will just ignore
  it. The server does not overwrite higher-indexed revisions.
- **Empty `revision_kind` on revisions.** Goldfive should stamp a
  kind on every revision beyond 0. An empty kind on a non-zero
  revision surfaces as an unattributed intervention in the
  aggregator; chase it upstream.
- **Plan id collisions across sessions.** Plans are scoped by
  `session_id`; `plan_id` is unique per session but not globally.
  When joining for cross-session analytics, include both keys.

## Cross-links

- `dev-guide/server.md#listinterventions-71` — aggregator internals.
- `dev-guide/storage-backends.md#task-plans` — schema columns.
- `user-guide/trajectory-view.md` — user-facing render.
- goldfive docs for the decision logic.
