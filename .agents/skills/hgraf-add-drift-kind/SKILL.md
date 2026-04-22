---
name: hgraf-add-drift-kind
description: Add a new drift kind end-to-end. Goldfive owns the detector + enum; harmonograf reflects the kind through the intervention aggregator and the UI timeline.
---

# hgraf-add-drift-kind

## When to use

You're adding a new *cause* the planner should see as a distinct
drift: a new tool-level detector, a new plan-coherence check, a new
user-control surface. The drift kind is a wire-visible string that
flows through goldfive into harmonograf and onto the intervention
timeline.

Examples of valid new kinds: "agent called a tool with stale args"
(new detector), "planner emitted a contradictory edge" (new validator
drift), "external signal X" (user-control variant). Not a new kind:
a slight variation of an existing detail (add a `detail` field to
an existing drift instead).

## Primary location: goldfive

Drift detection, the `DriftKind` enum, severity, and the classifier
all live in [goldfive](https://github.com/pedapudi/goldfive). Follow
goldfive's `how-to-add-a-drift-kind` guide first. The relevant
goldfive touchpoints:

- `proto/goldfive/v1/types.proto` — `DriftKind` enum.
- `proto/goldfive/v1/events.proto` — `DriftDetected` event shape;
  note `annotation_id` (#177) for user-control drifts.
- `goldfive.detectors.*` — the detector classes. `ToolLoopTracker`
  (#181/#186) for loops, the three-stage function_call gate (#178)
  for PLAN_DIVERGENCE / CONFABULATION_RISK.
- `goldfive.steerer` — user-control drift synthesis from incoming
  STEER / CANCEL controls.

Goldfive is the source of truth. If you only add the enum on the
goldfive side and ship, harmonograf will pick it up automatically
via `goldfive_event.drift_detected` ingest. The UI renders unknown
kinds generically; to make them first-class, continue with this
skill.

## Harmonograf-side steps

### 1. Server: intervention aggregator

`server/harmonograf_server/interventions.py` derives interventions
from annotations + drifts + plan revisions. Most of it is
tree-agnostic — any kind name renders. The exceptions:

- `_USER_DRIFT_KINDS` — the set of lowercase drift kinds that flag
  `source="user"` on the intervention row. If your new kind is a
  *user-control* drift emitted by goldfive from a STEER-like
  control, add its lowercase name here.
- `_GOLDFIVE_REVISION_KINDS` — the set of lowercase *revision* kinds
  (not drift kinds) that flag `source="goldfive"` autonomous
  intervention. Add if relevant.

### 2. Server: attribution window

`_outcome_window_for(kind)` returns 300 s for user-control kinds
(planner refine latency; #86) and 5 s for autonomous kinds. If your
new kind can block on an LLM call in goldfive — like a STEER waits
on the planner — route it through the user-control window path so
its attribution doesn't strand.

### 3. Frontend: deriver mirror

`frontend/src/lib/interventions.ts` mirrors the server aggregator.
Keep the two in lockstep:

- `USER_DRIFT_KINDS` — must match `_USER_DRIFT_KINDS` on the server.
- `GOLDFIVE_REVISION_KINDS` — must match `_GOLDFIVE_REVISION_KINDS`.
- `outcomeWindowFor(driftKind)` — mirrors the server's
  `_outcome_window_for`.
- `normalizeDriftKind(raw)` — lowercase kind → display label. Only
  the user-control kinds get pretty labels (`STEER`, `CANCEL`);
  drift and goldfive kinds are upper-cased verbatim.

### 4. Frontend: intervention timeline glyph

`frontend/src/components/Interventions/InterventionsTimeline.tsx`'s
`glyphFor(row)` picks marker shapes by source + kind. The default:

- user → diamond / diamond-x
- drift → circle or chevron (chevron when `outcome` is
  `plan_revised:*`)
- goldfive → square

If your kind deserves a new glyph (rare — the source trichotomy is
usually enough), extend the `Glyph` union and add a `<path>` branch
to the `Marker` component.

### 5. Tests

- `server/tests/test_interventions_aggregator.py` — add a case
  covering the new kind's source classification, outcome attribution,
  and dedup by annotation_id.
- `frontend/src/__tests__/interventions.test.ts` — add the mirror
  test for the client-side deriver.
- If you extended `glyphFor`, add a render-snapshot test for the new
  glyph.

## Verification

```bash
# Server unit
cd server && uv run pytest tests/test_interventions_aggregator.py -q

# Full server + client
make test

# Frontend
cd frontend && pnpm test -- --run intervention

# Live smoke
make demo
# Drive a scenario that emits the new drift; confirm marker renders
# on the InterventionsTimeline strip with correct source + glyph.
```

## Common pitfalls

- **Drift kind wire key drift.** The goldfive enum name (uppercased)
  is the wire value. Lowercase-compare across tables. Don't change
  it after ship — persisted sqlite `task_plans.revision_kind` rows
  hold the string.
- **Aggregator window mismatch.** Server and frontend both compute
  attribution windows. If you add a user-control kind and only
  update one side, the intervention dedup will be asymmetric and
  produce bonus cards.
- **annotation_id propagation.** User-control drifts must carry
  `annotation_id` (goldfive#177) so the aggregator folds the drift
  onto the source annotation. If a drift should be user-control but
  doesn't surface its annotation id, the intervention timeline will
  show duplicate cards.
- **Routing through goldfive.** Don't add detectors to harmonograf.
  Every drift path lives in goldfive post-migration.

## Cross-links

- `docs/user-guide/tasks-and-plans.md#drift-kinds` — user-facing
  taxonomy; update when adding a new kind.
- `docs/user-guide/trajectory-view.md` — how drifts surface on the
  intervention timeline.
- `docs/dev-guide/server.md#listinterventions-71` — aggregator
  internals.
