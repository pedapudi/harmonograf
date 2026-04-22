---
name: hgraf-update-planner-prompt
description: Planner prompts live in goldfive. This skill redirects and describes the harmonograf-side touchpoints for planner output changes.
---

# hgraf-update-planner-prompt

## Post-migration scope

The planner is in
[goldfive](https://github.com/pedapudi/goldfive). Harmonograf no
longer owns `client/harmonograf_client/planner.py`. Prompt changes,
schema tweaks, and refine-path logic all belong in goldfive.

If you want to change:

- How plans are initially generated → goldfive's planner prompt.
- How refines are structured → goldfive's steerer + planner.
- The `PlanSubmitted` / `PlanRevised` event schema →
  `proto/goldfive/v1/events.proto`.

## Harmonograf-side touchpoints

You only come back here if the output shape of a goldfive event
changes in a way that harmonograf's ingest or frontend needs to
learn about:

### 1. Ingest dispatch

`server/harmonograf_server/ingest.py`'s `_handle_goldfive_event`
fan-out covers `run_started` / `goal_derived` / `plan_submitted` /
`plan_revised` / `task_*` / `drift_detected` / `run_completed` /
`run_aborted`. New variants need a case; new field on an existing
variant usually just rides through in `convert.py`.

### 2. Storage columns

If the planner starts emitting a new field we want to persist
(e.g. a confidence score or plan digest), add it to
`storage/base.py`'s `TaskPlan` dataclass and `sqlite.py`'s CREATE
TABLE + conditional ALTER TABLE. See `hgraf-migrate-sqlite-schema`.

### 3. Frontend rendering

The frontend consumes plan shape via
`frontend/src/rpc/goldfiveEvent.ts` and `TaskRegistry`. If the new
field affects rendering — revision severity colour, banner text,
DAG layout hints — you'll need to thread it through.

## Testing

Harmonograf-side planner changes are exercised by:

- `server/tests/test_ingest_goldfive_event.py` — conversion round-trip.
- `server/tests/test_task_plans.py` — storage CRUD.
- Frontend: `frontend/src/__tests__/interventions.test.ts` if the
  aggregator cares.

Planner prompt regression goes in goldfive's test suite. Harmonograf
CI does not gate on planner output quality.

## Cross-links

- goldfive's planner docs (authoritative).
- `hgraf-migrate-sqlite-schema` for persistence.
- `dev-guide/server.md` for the ingest dispatch.
