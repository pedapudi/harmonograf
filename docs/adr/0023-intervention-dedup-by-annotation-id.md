# ADR 0023 — Intervention dedup by `annotation_id`

## Status

Accepted (2026-04, harmonograf #71 / #75 / #81 / #87, goldfive #171 / #176 / #177).

## Context

An intervention — a point in a run where the plan changes direction —
can surface on the wire from three different primitives, often all at
once for a single user action:

1. A **`PostAnnotation`** arrives with kind=STEERING. It persists as an
   `annotations` row and is routed through the control path as a
   `STEER` event.
2. Goldfive receives the STEER, flips its drift detector, and emits a
   **`DriftDetected`** event with kind=`user_steer`.
3. Goldfive's planner refines the plan and emits a
   **`PlanRevised`** event whose `revision_kind=user_steer`.

Without dedup, a single user STEER surfaces as **three** cards in the
Intervention timeline: an annotation row, a drift row, and a plan
revision row. Operators saw this as noise and couldn't tell which was
the "real" event.

## Decision

Every stop on the pipeline that can carry a back-reference to the
source annotation does so:

- `goldfive.v1.SteerPayload` adds `author` (field 3) and
  `annotation_id` (field 4). Harmonograf's `PostAnnotation` handler
  populates both from the stored annotation row when it synthesizes
  the STEER event (see
  [`server/harmonograf_server/rpc/frontend.py`](../../server/harmonograf_server/rpc/frontend.py)).
- `goldfive.v1.DriftDetected` adds `annotation_id` (field 6).
  Goldfive stamps it when a user-originated drift propagates from a
  STEER that carried one.
- `TaskPlan.revision_kind` and the drift that triggered a refine
  carry the annotation_id forward so the plan revision row can also
  be joined on it.

The aggregator (`server/harmonograf_server/interventions.py`)
collapses all rows that share an `annotation_id` into a single
user-authored card, merging outcome, severity, and plan-revision
metadata onto the annotation-derived row. Autonomous drifts (no
annotation_id, e.g. `looping_reasoning`) keep their own cards.

The frontend deriver (`frontend/src/lib/interventions.ts`) mirrors
the aggregator for live updates: a STEER annotation, drift, and plan
revision all arriving on the live stream collapse into one row
incrementally without a server round-trip.

## Consequences

**Good.**
- One user STEER = one card. Operators see exactly the set of
  distinct interventions without double-counting.
- The dedup contract is structural, not temporal: it survives out-of-
  order arrival, reconnects, and late drift emissions.
- Autonomous drifts (ones goldfive mints on its own) keep their own
  rows automatically, because they never carry an `annotation_id`.
- Forward-compatible: if goldfive later introduces a new user-control
  kind (e.g. `user_rewind`), stamping the annotation_id on the
  resulting drift is enough — the aggregator doesn't need code
  changes.

**Bad.**
- The attribution window is heuristic. A drift that legitimately
  fires within 5 minutes of a user STEER but is unrelated to it (e.g.
  a separate loop-detection drift) is attributed to the STEER's
  outcome column. The 5-minute window (for user-control kinds;
  autonomous kinds use a tight 5s) was tuned against real sessions;
  shorter windows dropped legitimate plan revisions on slow LLMs,
  longer windows caused false merges.
- Every layer (harmonograf wire, goldfive wire, sink, aggregator,
  frontend deriver) has to thread the `annotation_id` through
  consistently. One missed propagation site reintroduces the triple
  card.
- Plans don't currently carry an `annotation_id` field — the join
  onto the plan row happens indirectly through the preceding drift.
  A user STEER that somehow skips the drift (the refine pipeline
  shortcuts) would leave the plan row unattributed. So far this
  path has not been observed in practice.

## Implemented in

- [`server/harmonograf_server/interventions.py`](../../server/harmonograf_server/interventions.py)
- [`server/harmonograf_server/rpc/frontend.py`](../../server/harmonograf_server/rpc/frontend.py) — `PostAnnotation` populates `SteerPayload.author` + `annotation_id`.
- [`frontend/src/lib/interventions.ts`](../../frontend/src/lib/interventions.ts) — client-side dedup mirror.
- Goldfive: `SteerPayload.author` / `annotation_id` (control.proto), `DriftDetected.annotation_id` (events.proto).
- [Design 13 — Human interaction model](../design/13-human-interaction-model.md).
