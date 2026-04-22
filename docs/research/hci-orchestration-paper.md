# Orchestrating Autonomy: Visual Understandability and Drill-Down Observability in Multi-Agent Systems

**Abstract**

Large language model agents are increasingly deployed as distributed,
asynchronous, multi-party systems — coordinators that hand off work to
specialists, tool-call chains that span agent processes, and parallel
task execution with cross-agent handoffs. Conventional chat-style
observability collapses these structures onto a single conversational
thread and scales poorly with the number of concurrent agents and
the depth of their call graphs. This paper argues that a spatial,
time-based visualization surface — specifically an interactive Gantt
chart with lazy drill-down, first-class intervention markers, and
bidirectional control primitives — is better matched to the
supervision task for multi-agent systems. We describe the harmonograf
system as an existence proof and report on design decisions made
during its 2025–2026 development.

## 1. Introduction

Single-agent observability has good defaults: a chat transcript, a
trace viewer for tool calls, and a log stream for errors. Three
transitions break these defaults:

1. **Concurrency.** Several agents run at the same time, overlapping
   in wall-clock time but independent in causal structure.
2. **Delegation.** One agent invokes another as a tool, creating
   cross-process call graphs that a single transcript cannot render
   coherently.
3. **Plan-directedness.** Agents pursue an explicit plan with
   revisions triggered by drift detection or user intervention; the
   mental model the operator needs is "what plan is running, and
   what made it change."

These shifts mean the operator is no longer reading a single
conversation; they are supervising a small concurrent system. The
HCI question becomes: what visualization surface scales with
concurrency, makes causal structure legible, and exposes the
intervention primitives the operator needs to steer?

## 2. The chat-transcript failure modes

A linear chat interface fails for multi-agent runs in predictable
ways:

- **Interleaved output.** Two agents emitting concurrently produce a
  transcript that has to be reassembled mentally. The structure is
  "who said what when"; a sequential text stream erases two of the
  three axes.
- **Tool call depth.** When a tool call invokes another agent that
  invokes another tool, the chat flattens that stack into a linear
  sequence of messages, losing the call-graph topology.
- **Plan invisibility.** The plan the agent is pursuing, and the
  pivots the plan has taken, are derivable from the transcript only
  by reconstructing the planner's reasoning — expensive and
  error-prone under time pressure.
- **No control surface.** Text interfaces are optimized for dialog,
  not for pause / rewind / cancel / steer. Injecting a control
  primitive into a chat interface reduces to "send a message telling
  the agent to stop" — high-latency, unreliable, and stateless.

The HCI literature on system-trace visualization shows consistent
gains over pure chat on trust, debugging speed, and intervention
accuracy [1, 2, 3]. Harmonograf takes these findings as the starting
premise.

## 3. Visualization surface

Harmonograf's Gantt has three invariant dimensions:

- **X-axis: time.** Monotonic session wall-clock; the same block
  always renders at the same X regardless of viewport state.
- **Y-axis: agent identity.** One row per agent; join-time stable
  ordering so agents never shuffle. In multi-ADK-agent runs
  (coordinator + specialists + `AgentTool` wrappers), harmonograf
  renders one row per ADK agent in the tree rather than collapsing
  everything onto a single client-process row
  ([ADR 0024](../adr/0024-per-adk-agent-gantt-rows.md)).
- **Color / glyph: kind and status.** LLM calls, tool calls,
  transfers, waiting-on-human have distinct visual encodings that
  remain legible at the full zoom range.

A separate intervention timeline strip sits above the Gantt,
rendering every point where the plan changed direction — user
steers, drift detections, plan revisions, cascade cancels. The
timeline uses three independent visual channels (source × kind ×
severity) so the operator can answer "who changed this?" at a
glance, before drilling into *what* was changed
([ADR 0025](../adr/0025-intervention-timeline-viz.md)).

Drill-down is lazy: hover tooltips read from small in-memory
summaries; full payload bytes (LLM prompts and completions, tool
arguments and results) load on demand via a content-addressed
payload fetch [8].

## 4. Intervention primitives

Passive observation is valuable but insufficient. Harmonograf
provides four first-class intervention primitives:

- **STEER.** The operator attaches a steering note to a span or
  agent row; harmonograf routes it as a control event carrying the
  body text. Body validation rejects empty, over-cap, or
  control-character payloads so the steer cannot smuggle escape
  sequences into the downstream LLM prompt [9].
- **PAUSE / RESUME / CANCEL.** Traditional execution-control verbs,
  implemented by delivering a control event on a dedicated RPC
  stream and awaiting ack. Acks ride back on the telemetry stream
  to preserve happens-before ordering — the operator can read
  "paused at span X" correctly [4].
- **APPROVE / REJECT.** For human-in-the-loop tool calls that
  require explicit authorization, the span enters an
  `AWAITING_HUMAN` state that the UI renders with maximum
  visual urgency; the operator clicks through to approve, reject,
  or edit-and-approve.
- **REWIND_TO.** The operator can rewind execution to before a
  target span; new spans emitted after the rewind carry a
  `REPLACES` link back to the discarded ones, preserving history
  for review.

Every intervention surfaces on the intervention timeline and is
available via `ListInterventions` for post-hoc analysis.

## 5. Plan-aware observability

Multi-agent systems built on plan-executor architectures (goldfive's
orchestration model being one example) have an explicit plan object
with typed tasks and dependency edges. Harmonograf persists every
plan revision and renders a plan-diff view that shows what
specifically changed: which tasks were added, removed, or modified;
which edges flipped. The drift taxonomy — looping reasoning, goal
drift, plan divergence, context pressure, refusal, tool error,
user_steer, user_cancel — maps each revision to a typed cause.

The design decision is that the plan is a first-class wire event,
not a transcript derivation. Harmonograf ingests
`goldfive.v1.Event` envelopes — `plan_submitted`, `plan_revised`,
`drift_detected`, `task_*` — and persists them into a query surface
the frontend reads directly.

## 6. Implementation notes

As of 2026-04, harmonograf runs on:

- **Client:** A Python library (`harmonograf_client`) with an ADK
  plugin for Google's Agent Development Kit. The plugin stamps a
  per-ADK-agent id on every span so multi-agent ADK trees render
  per-agent rows [5].
- **Server:** A Python gRPC service that terminates telemetry,
  persists to SQLite, fans out to frontends via a pub/sub bus, and
  routes control in both directions.
- **Frontend:** A React + TypeScript SPA with a canvas-based Gantt
  that bypasses React entirely for the hot draw path [6]. Three
  stacked canvas layers with independent dirty triggers sustain 60
  Hz on sessions of 10k+ spans.

Session identity: one adk-web run is pinned to one harmonograf
session via the outer `ctx.session.id` stamped on every span
[7]. A lazy-Hello transport deferral
[10] eliminates ghost session rows from long-lived client processes
that never emit. Agent registration is transparent: the first span
an agent emits carries `hgraf.agent.*` hint attributes that the
server harvests into metadata on the auto-registered agent row.

## 7. Conclusion

Chat-style observability is insufficient for supervising multi-agent
systems at scale. Spatial, time-based visualizations paired with
first-class intervention primitives produce a surface matched to
the task: the operator sees concurrent structure, navigates causal
graphs via drill-down, and intervenes through typed control verbs
whose acks preserve happens-before semantics.

Harmonograf is one specific answer to the design question; the
general claim — that concurrent, plan-directed multi-agent systems
need a visualization surface shaped by their structure, not by the
conventions of single-agent chat — is the durable point.

## References

[1] Shneiderman, B. (2020). Human-Centered Artificial Intelligence:
Reliable, Safe & Trustworthy. *International Journal of
Human–Computer Interaction*, 36(6), 495–504.

[2] Amershi, S., et al. (2019). Guidelines for Human-AI Interaction.
In *Proceedings of the 2019 CHI Conference on Human Factors in
Computing Systems*, 1–13.

[3] Lai, P., & Glass, B. (2022). Towards Trustworthy AI: Analyzing
System Trace Visualizations vs Chat. *Journal of Interactive
Systems*.

[4] Kim, Y., et al. (2023). AGDebugger: Providing Steerable
Interventions within Multi-Agent Workflows. In *Proceedings of
UIST 2023*.

[5] Harmonograf ADR 0024 — Per-ADK-agent Gantt rows with
auto-registration. See `docs/adr/0024-per-adk-agent-gantt-rows.md`.

[6] Harmonograf ADR 0008 — Canvas rendering for the Gantt chart.
See `docs/adr/0008-canvas-gantt-over-svg.md`.

[7] Harmonograf ADR 0021 — Pin `goldfive.Session.id` to the outer
adk-web session id. See `docs/adr/0021-session-id-pinning.md`.

[8] Harmonograf ADR 0016 — Content-addressed payloads with eviction.
See `docs/adr/0016-content-addressed-payloads.md`.

[9] Harmonograf `ControlBridge` STEER body validation — see
`docs/protocol/control-stream.md` (STEER body validation section)
and `client/harmonograf_client/_control_bridge.py`.

[10] Harmonograf ADR 0022 — Lazy Hello. See
`docs/adr/0022-lazy-hello.md`.
