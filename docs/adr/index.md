# Architecture Decision Records

This directory holds the architecture decision records (ADRs) that document
the major design choices that shaped harmonograf. Each ADR follows a
standard format — Title, Status, Context, Decision, Consequences — and is
meant to be readable in isolation: a future contributor (human or agent)
should be able to read any one ADR and understand *why* that piece of
harmonograf is shaped the way it is, without having to spelunk commit
history or reverse-engineer proto files.

> **Post-migration note (2026-04).** ADRs 0011, 0011a, 0012, 0013, 0014,
> 0015, 0017, and 0019 cover orchestration concerns that moved to
> [goldfive](https://github.com/pedapudi/goldfive) during the
> harmonograf → goldfive split. Each has a deprecation / superseded
> banner at the top. The reasoning is preserved as historical record;
> the decisions themselves now live in goldfive's docs. Read
> [../goldfive-integration.md](../goldfive-integration.md) first if you
> are here to understand the current shape rather than the archaeology.

## Reading order

If you are new to harmonograf, read in this order:

1. [ADR 0001](0001-why-harmonograf.md) (motivation) and [ADR 0002](0002-three-component-architecture.md) (component split) together — they
   give the "why does this project exist" and "what are the parts" view.
2. [ADR 0003](0003-adk-first.md) (ADK first) and [ADR 0014](0014-session-state-as-coordination-channel.md) (session.state) — how we integrate
   with the host framework.
3. [ADR 0010](0010-span-is-not-task.md) (task vs span), [ADR 0011](0011-reporting-tools-over-span-inference.md) (reporting tools), and [ADR 0017](0017-monotonic-task-state.md)
   (monotonic task state) — the core of the plan-execution protocol.
   [ADR 0011a](0011a-span-lifecycle-inference-superseded.md) is the superseded predecessor of [ADR 0011](0011-reporting-tools-over-span-inference.md) and explains
   what was tried and why it broke.
4. [ADR 0004](0004-telemetry-control-split.md), [0005](0005-acks-ride-telemetry.md), [0006](0006-grpc-over-other-transports.md) — the wire protocol shape (telemetry/control
   split, ack riding semantics, gRPC choice).
5. Everything else, by interest.

## Index

- [0001 — Why harmonograf exists](0001-why-harmonograf.md) — multi-agent
  systems are invisible to span-based observability; a plan-aware,
  explicit-state, bidirectional console is a different product.
- [0002 — Three-component architecture](0002-three-component-architecture.md)
  — frontend / client library / server split; one server, many agents,
  many viewers.
- [0003 — ADK as the first-class integration target](0003-adk-first.md)
  — why ADK is the v0 adapter and how the core stays framework-agnostic.
- [0004 — Telemetry and control are separate RPCs](0004-telemetry-control-split.md)
  — per-stream flow control keeps a PAUSE from being stuck behind a
  payload upload.
- [0005 — Control acks ride upstream on the telemetry stream](0005-acks-ride-telemetry.md)
  — happens-before for free from in-order bytes on one stream.
- [0006 — gRPC as the wire transport](0006-grpc-over-other-transports.md)
  — one proto schema for three components, streaming native and browser.
- [0007 — SQLite as the v0 timeline store](0007-sqlite-over-postgres.md)
  — zero-install fits the single-server-process model; store is behind
  an interface.
- [0008 — Canvas rendering for the Gantt chart](0008-canvas-gantt-over-svg.md)
  — SVG/DOM does not scale to the target workload; accessibility is the
  cost.
- [0009 — UUIDv7 for span identifiers](0009-uuidv7-span-ids.md)
  — sortable, client-side, dedup-on-reconnect friendly.
- [0010 — A span is not a task](0010-span-is-not-task.md) — why tasks and
  spans are separate first-class primitives.
- [0011 — Reporting tools drive task state, not span lifecycle](0011-reporting-tools-over-span-inference.md)
  — the iter15 pivot: declared transitions over inferred ones.
- [0011a — Span-lifecycle inference (Superseded)](0011a-span-lifecycle-inference-superseded.md)
  — the predecessor of [ADR 0011](0011-reporting-tools-over-span-inference.md); what iter14 tried and why it broke.
- [0012 — Three orchestration modes](0012-three-orchestration-modes.md)
  — sequential, parallel, delegated — each appropriate for a different
  execution pattern.
- [0013 — Drift is a first-class event](0013-drift-as-first-class.md) —
  drift taxonomy, refine as product primitive, plan diffs render to the
  operator.
- [0014 — `session.state` is the coordination channel](0014-session-state-as-coordination-channel.md)
  — bidirectional coordination reusing ADK's own shared dict.
- [0015 — Ship the invariant validator to production](0015-invariants-as-safety-net.md)
  — per-write guards plus aggregate validation catch compounding bugs.
- [0016 — Content-addressed payloads with eviction](0016-content-addressed-payloads.md)
  — out-of-band uploads, sha256 addressing, dedup, eviction as a
  first-class state.
- [0017 — Task state is monotonic; terminal states absorb](0017-monotonic-task-state.md)
  — stale messages cannot corrupt state; refines replace tasks rather
  than mutate terminal ones.
- [0018 — Heartbeat + progress_counter for stuck detection](0018-heartbeat-stuck-detection.md)
  — distinguishes "slow" from "stuck"; powers the live-activity tooltip.
- [0019 — `HarmonografAgent` and `HarmonografAdkPlugin` are separate](0019-plugin-agent-split.md)
  — orchestration and telemetry are separate ADK extension points;
  compose them.
- [0020 — No authentication or multi-tenancy in v0](0020-no-auth-in-v0.md)
  — loopback-first plus optional shared bearer token; auth v1 is a
  future concern.
- [0021 — Pin `goldfive.Session.id` to the outer adk-web session id](0021-session-id-pinning.md)
  — one adk-web run = one harmonograf session, even with AgentTool
  sub-Runners minting their own session ids.
- [0022 — Lazy Hello](0022-lazy-hello.md) — defer the `Hello` RPC
  until the first real emit; eliminates ghost `sess_*` rows and lets
  the home session stamp with the correct id from the start.
- [0023 — Intervention dedup by `annotation_id`](0023-intervention-dedup-by-annotation-id.md)
  — one user STEER can surface as an annotation, a drift, and a
  plan revision; dedup structurally on the source annotation id.
- [0024 — Per-ADK-agent Gantt rows with auto-registration](0024-per-adk-agent-gantt-rows.md)
  — per-agent id stacking in the plugin + first-span `hgraf.agent.*`
  hints + server-side auto-register give one Gantt row per ADK
  agent without a new wire event.
- [0025 — Intervention timeline viz on three channels](0025-intervention-timeline-viz.md)
  — source (color) × kind (glyph) × severity (ring) plus stable
  X-anchor hover; colorblind-safe by construction.

## Writing a new ADR

When a non-trivial design decision ships, add an ADR. Rules of thumb:

- **Write it when the decision lands, not months later.** The context —
  what alternatives you considered and why you rejected them — decays
  fast.
- **Be honest about the downsides.** Every decision has costs. If an
  ADR's Consequences section is all "good," it is undersold. A future
  reader needs to know the costs so they can judge when the decision
  is worth revisiting.
- **300-800 words is the target.** Longer only if the topic genuinely
  demands it.
- **Status of shipped decisions is `Accepted`.** If a decision is later
  reversed, mark the original `Superseded by ADR-N` and write the new
  ADR explaining what replaced it and why. Do not delete the
  superseded ADR — the reason it was wrong is load-bearing for future
  readers considering the same shortcut (see [ADR 0011a](0011a-span-lifecycle-inference-superseded.md) as the
  template).
- **Ground truth is code + proto comments + AGENTS.md + git history.**
  Cite file paths and commit SHAs where it helps.
