# ADR 0025 — Intervention timeline visualization: three-channel encoding

## Status

Accepted (2026-04, harmonograf #76).

## Context

The Intervention timeline (see
[ADR 0023](0023-intervention-dedup-by-annotation-id.md)) renders a
horizontal strip of markers for every intervention in a session. A
single strip has to communicate three dimensions at a glance:

- **Source** — was this a user action, a drift detector, or a
  goldfive autonomous escalation?
- **Kind** — what kind of action (STEER vs CANCEL vs PAUSE for
  user-authored; which drift kind for drift-authored; etc.)?
- **Severity** — does this need the operator's attention right now
  (critical red ring) or is it informational?

Early designs collided these into one visual channel (color per
kind). It failed on colorblind accessibility and also failed on
"drift vs goldfive vs user" being the usually-interesting axis —
operators wanted to scan for "who did this, the user or the
system?" before they cared about the specific kind.

A second problem: live sessions advance `endMs` every frame, and
if the marker X position recomputed from `endMs` on every render,
hovering a marker shifted the others left by a few pixels, making
the popover anchor drift and clustering boundaries jitter.

## Decision

The `InterventionsTimeline` component encodes the three dimensions
on three independent visual channels:

1. **Source → color.** One color per source, sampled from a
   deliberately limited palette so the legend is legible at a
   glance:
   - `user` → blue (`#5b8def`)
   - `drift` → amber (`#f59e0b`)
   - `goldfive` → grey (`#8d9199`)
2. **Kind → glyph.** Diamond / circle / chevron / square, assigned
   per kind within a source. Each source has its own glyph set so
   a user's "STEER diamond" and a drift's "diamond" don't look
   identical.
3. **Severity → ring.** Markers get a dashed amber ring at
   `warning`, a solid red ring at `critical`. No ring at `info` or
   when severity is absent.

Clustering: two markers within `max(14px, 2% of strip width)` of
each other collapse into a single "N" cluster badge whose popover
lists the group. The density threshold is a floor so zoomed-in
strips don't cluster aggressively.

Stability: the component captures `endMs` as a `spanEndMs` snapshot
on mount and advances it on a coarse 1s tick via
`useStableSpanEnd`. Hover state changes never cause the X-axis
anchor to recompute, so hovering one marker never shifts the
others.

The popover is deterministically anchored to the marker's center,
not the cursor. Axis ticks auto-select from a fixed ladder (10s,
30s, 1m, 5m, 10m, 30m) based on the visible window.

See
[`frontend/src/components/Interventions/InterventionsTimeline.tsx`](../../frontend/src/components/Interventions/InterventionsTimeline.tsx)
and `SOURCE_COLOR` in
[`frontend/src/lib/interventions.ts`](../../frontend/src/lib/interventions.ts).

## Consequences

**Good.**
- Three independent channels mean source, kind, and severity can
  all be read separately without legend gymnastics. Operators
  learn "amber means a drift" on first sight.
- Colorblind accessibility: any two of the three channels survive
  any common color-vision condition. A deuteranopic user who can't
  distinguish amber from grey still sees the glyph and the ring.
- The stable X anchor is the single most-requested fix from early
  user testing. Hovering never jitters the strip.
- Tree-agnostic: the component never inspects kind taxonomies or
  source-specific strings. New drift kinds, new user-authored
  kinds, new goldfive escalation kinds all render identically
  without code changes.
- Popover stability survives rapid marker density: the cluster
  badge is anchored too, so hovering a cluster that overlaps
  another cluster doesn't shuffle them.

**Bad.**
- Four glyphs per source is a hard cap before distinct shapes
  start colliding. If the intervention taxonomy grows past that,
  the strip will have to reduce glyph variety or split per-source.
- The 2% clustering floor is heuristic. Very wide strips (24"
  monitors) can under-cluster; very narrow strips (side-panel
  width) can over-cluster. No per-user tuning.
- Severity ring semantics ("warning = dashed amber, critical =
  solid red") duplicate information for user-authored interventions
  — a user STEER is never critical — but the aggregator is source-
  agnostic so the ring logic is applied uniformly. The redundancy
  is minor and the alternative (per-source severity encoding) was
  worse.
- The stable X anchor means the rightmost edge of the strip doesn't
  track the session's live-ness in real time. The ~1s snapshot
  cadence is fine for the intervention use case but would be wrong
  if the strip were re-used to show, e.g., per-second LLM token
  output.

## Implemented in

- [`frontend/src/components/Interventions/InterventionsTimeline.tsx`](../../frontend/src/components/Interventions/InterventionsTimeline.tsx)
- [`frontend/src/lib/interventions.ts`](../../frontend/src/lib/interventions.ts)
- [Design 04 — Frontend and interaction](../design/04-frontend-and-interaction.md)
- [Design 13 — Human interaction model](../design/13-human-interaction-model.md)
