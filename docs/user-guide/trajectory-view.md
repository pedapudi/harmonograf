# Trajectory view

The Trajectory view (`Trajectory` in the nav rail) is harmonograf's dedicated
surface for **plan review, steering, and intervention history**. Where the
Gantt answers "what happened, when?", the Trajectory answers "how did this
plan get from rev 0 to where it is now, and what nudges (human, drift,
goldfive) pushed it along the way?".

It unifies three things into one pane:

- The **plan DAG** — the current revision rendered as a layered
  left-to-right topological layout.
- The **intervention timeline** — every user STEER, every drift, every
  autonomous goldfive revision, as markers on a horizontal strip above
  the DAG.
- A **detail pane** on the right that shows whatever you last clicked.

## When to use it

The Gantt shows a ticker-tape of spans. That works well when you're chasing a
specific invocation or looking at overall pacing, but it hides two things
plan-driven runs really care about:

- **Plan structure.** Which tasks depend on which? Where is the DAG branchy
  vs. linear? A flat task panel can't show dependencies at a glance.
- **How the plan evolved under outside pressure.** Between the initial plan
  and "now" there may be several refines, each triggered by a human STEER,
  a drift detector fire, or goldfive's own intervention ladder (cascade
  cancels, refine retries, human-intervention-required escalations). The
  Gantt shows the *effect* of each refine but not the *decision*.

The Trajectory is the pane you open to answer "why is the plan shaped like
this?" or "what did rev 2 change about rev 1?" or "what was the human
steering here?".

## The intervention timeline strip

The spine of the view is the **InterventionsTimeline** (`#71`, redesigned
in `#76`) — a horizontal strip of markers showing every intervention in
chronological order. It also sits above the Gantt in the main view, so
the same vocabulary applies everywhere.

### Three channels: glyph · color · ring

Every marker uses three orthogonal visual channels:

| Channel | Drives | Values |
|---|---|---|
| **Color (fill)** | *source* — who initiated | `user` (purple), `drift` (amber), `goldfive` (grey) |
| **Glyph** | *kind* — what was done | diamond = user STEER, diamond-x = user CANCEL, chevron = drift that caused a plan revise, circle = drift that did not cause a revise, square = autonomous goldfive action |
| **Ring** | *severity* — how loud | no ring = info, dashed = warning, solid red = critical |

New drift kinds emitted by goldfive tomorrow render correctly without any
frontend change: the strip never inspects kind taxonomies. It only knows
the source trichotomy, which is assigned once by the deriver.

### Stable X anchor

The marker X positions are computed once per session-relative time window
and held steady. Hovering a marker will *not* shift its neighbors even
when the session is live and the parent's end-of-window is advancing
every frame. The strip advances on a coarse 1-second tick only.

### Density clustering

Markers whose centers fall within ~2 % of the strip width of each other
collapse into a single cluster badge. Hover the badge to see the group;
click to pin its popover. Typical sighting: a cascade-cancel that
triggers five CANCELLED tasks at once.

### Deterministic popover

The popover is anchored to the marker's x position, not the cursor — so
moving the mouse inside the popover doesn't drag it around, and clicking
a marker pins the popover (click again to unpin, or click outside the
strip).

### Axis ticks

4-8 session-relative time labels (`10s`, `30s`, `1m`, `5m`, …) render
along the bottom of the strip so you can orient the markers in time at a
glance.

## Card anatomy (single-row popover)

Clicking or hovering a marker opens a popover with these fields:

| Field | Source | What it tells you |
|---|---|---|
| **Source glyph + label** | `source` | "User", "Drift", or "Goldfive" with the matching color. |
| **Kind** | `kind` | `STEER` / `CANCEL` / `LOOPING_REASONING` / `TOOL_ERROR` / `CASCADE_CANCEL` / `REFINE_RETRY` / … |
| **Timestamp** | `at` | Session-relative `m:ss` (e.g. `2:34`). |
| **Body preview** | `body_or_reason` | For user STEER: the operator's note. For drift: the detector's detail. First ~200 chars; click **Show full** for the rest. |
| **Author** | `author` | For user-sourced: the poster (default `user`). Empty for drift / goldfive. |
| **Severity ring** | `severity` | Echoes the marker ring — `info` / `warning` / `critical`. |
| **Outcome chip** | `outcome` | `→ rev 3` when a plan revision was attributed, `→ cancel 2 tasks` when a cascade cancel fired, `pending` while goldfive is still refining, `recorded` when no follow-up was correlated. |
| **Jump to rev** | — | If the outcome is `plan_revised:rN`, a button jumps the DAG pane to that revision. |

### Intervention cards dedup by annotation_id

A single user STEER used to surface as three cards: the annotation
itself, the USER_STEER drift goldfive emitted, and the `plan_revised:rN`
that followed. As of `#81` / `#87`, the aggregator collapses all three
onto one card — the annotation row — whose outcome carries the revision
attribution. This works even when the planner's refine LLM takes 30-70
seconds (Qwen3.5-35B often does), thanks to an extended 5-minute
attribution window scoped to user-control drifts (`#86`).

## Filtering

The drawer's **Interventions** tab lists the same rows vertically with
filter chips:

- **Source:** user · drift · goldfive
- **Severity:** info · warning · critical
- **Outcome:** plan revised · cascade cancel · pending · recorded

Filters compose. Picking `user + critical` shows only operator
intervention cards that escalated a severity-critical outcome.

## The DAG pane

The DAG below the strip is a left-to-right Kahn topological layout:
every task's horizontal position is its longest-path distance from a
root, so edges always go right. Within a layer, tasks stack vertically.

Each card shows:

- **Title** on the first line.
- **Status and assignee** on the second line (e.g. `completed · worker`).
- **A 6 px color bar** on the left edge encoding task status:

  | Status | Color |
  |---|---|
  | `PENDING` / `UNSPECIFIED` / `NOT_NEEDED` / `CANCELLED` | grey |
  | `RUNNING` | blue |
  | `COMPLETED` | green |
  | `FAILED` | red |
  | `BLOCKED` | amber |

- **A severity-colored drift badge** in the top-right corner when one or
  more drifts point at that task.

Click a card to select the task. The detail pane shows its description,
assignee, bound span, and drifts-on-this-task. If `bound_span_id` is
set, the inspector drawer also opens on that span.

## Diffing two revs

Shift-click any rev chip to pin it as a **compare rev**. The current rev
renders with diff marks overlaid: added tasks get a green dashed outline,
modified tasks get a blue dashed outline, removed tasks appear in a
"Removed in rev" aside on the right so they don't corrupt the current-rev
layout. Click `clear diff` to exit.

## Live updates

The Trajectory view live-subscribes to the same registries the Gantt does
(`store.spans`, `store.drifts`, `store.tasks`, `annotationStore`) and
re-derives the intervention list via `lib/interventions.ts` on every
WatchSession delta. As the agent runs:

- New annotations and drifts materialize as markers on the strip
  (fade-in once; subsequent renders don't replay the entrance).
- Plan revisions push new chips into the rev counter.
- Status changes repaint the corresponding card on the DAG.

If you've pinned a specific rev (clicked a chip), the live pin *sticks* —
the ribbon grows, but the DAG and detail pane stay on the pinned rev.

## Interaction summary

| Input | Result |
|---|---|
| Hover a marker | Preview popover at the marker's anchor position. |
| Click a marker | Pin the popover. Click again (or outside) to unpin. |
| Hover/click a cluster badge | Show the group. |
| Click a rev chip | Pin that rev as the current rev. |
| Shift-click a rev chip | Pin as compare rev (diff mode). |
| Click `clear diff` | Exit diff mode. |
| Click a task card | Pin it in the detail pane; open the drawer on the bound span if one exists. |
| Click the `→ rev N` chip on a popover | Jump the DAG pane to that revision. |

## Related pages

- [Tasks and plans](tasks-and-plans.md) — the task state machine and
  the drift kind taxonomy harmonograf reflects from goldfive.
- [Control actions](control-actions.md) — how to STEER / CANCEL a run;
  the cards you see here are the receipts for those actions.
- [Annotations](annotations.md) — how STEER / HUMAN_RESPONSE / COMMENT
  are authored and stored.
- [Gantt view](gantt-view.md) — the other half of the picture (spans,
  per-agent rows, cross-agent edges).
