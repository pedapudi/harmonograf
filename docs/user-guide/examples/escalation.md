# Scenario: multi-agent team with escalation to human review

Three agents: a `coordinator` that delegates to a `researcher` and a
`writer`. The writer hits a passage that needs a factual judgment it is
not confident about, and escalates to human review via a `WAIT_FOR_HUMAN`
span. You approve from the drawer's Control tab, the writer unblocks,
the plan completes.

## Set-up

- Agents: `coordinator` (sequential), `researcher`, `writer`.
- Topology: coordinator delegates to researcher, then writer. Writer
  escalates mid-draft.
- All three advertise `PAUSE_RESUME` and `STEERING`. Approve / reject
  are always available regardless of capabilities (see
  [control-actions.md → capability negotiation](../control-actions.md#capability-negotiation)).
- Framework: ADK with `HarmonografAgent` wrapping the coordinator.

## Timeline

### t=0 — all three agents connect

After each `Hello`, the session picker row for this session ticks up
the agent count (`3 agents`). The Gantt opens with three rows; the
coordinator has an open INVOCATION, the other two are idle.

The [Graph view](../graph-view.md) (◈ in the nav rail) shows three
columns with headers. Green status dots, no borders.

### t=3s — coordinator transfers to researcher

A TRANSFER span opens on `coordinator`'s row. Its `INVOKED` link points
at a new INVOCATION on `researcher`. On the Gantt this draws as a bezier
edge from the coordinator row down into the researcher row. On the
[Graph view](../graph-view.md#arrows--transfer-delegation-return) it is
an orange solid 2.5 px arrow with the label "delegate: research".

![TODO: screenshot of the Graph view at t=3 with the transfer arrow](_screenshots/example-escalation-transfer.png)

### t=20s — researcher completes, returns to coordinator

The researcher's INVOCATION closes. A grey dashed "return" arrow draws
back from the researcher column to the coordinator column on the Graph
view.

### t=22s — coordinator transfers to writer

Second orange arrow, this time to the writer column. The writer opens
an INVOCATION and starts a long LLM_CALL. Streaming ticks appear on the
trailing edge.

### t=58s — writer opens a WAIT_FOR_HUMAN span

Writer is halfway through a draft and decides one sentence about patent
law needs human sign-off. It opens a `WAIT_FOR_HUMAN` span under its
INVOCATION and transitions the span to `AWAITING_HUMAN`.

What you see in the UI:

- A new bar appears on `writer`'s row. Kind `WAIT_FOR_HUMAN`, status
  `AWAITING_HUMAN`. Red outline + 1 s pulse + error-container fill. The
  glyph at widths ≥ 12 px is `⏸`.
- The **attention badge** on the app bar ticks up. The session row in
  the picker gets a red `1 needs attention` chip. See
  [sessions.md → attention badges](../sessions.md#attention-badges).
- `drift_kind = "agent_escalated"` on the active INVOCATION (the
  writer's). This pill also fires on the PlanRevisionBanner if the
  planner converts the escalation into a plan revision — not every
  setup does.

### t=60s — operator finds and inspects the escalation

You notice the red chip and open the session. The Gantt scrolls to the
live edge (live-follow is on by default after picking a session). You
see the red pulsing bar on the writer's row.

Click the bar. The drawer opens. What you look at:

1. **Summary tab.** `status = AWAITING_HUMAN`, `agent = writer`. The
   attributes table shows whatever metadata the writer stamped:
   `question`, `context`, `options`. See
   [drawer.md → summary tab](../drawer.md#summary-tab).
2. **Payload tab.** The writer attached the full question as a
   payload. Click **Load full payload** (payloads are not fetched
   eagerly, see [drawer.md → payload tab](../drawer.md#payload-tab)).
   JSON pretty-prints.

### t=72s — operator approves

Switch to the drawer's **Control** tab. Because the span is
`AWAITING_HUMAN`, the `Approve` and `Reject` buttons are visible (they
only render for that status — see
[drawer.md → control tab](../drawer.md#control-tab)).

Click **Approve**. The frontend sends a `ControlEvent` with
`kind = APPROVE`, empty payload, target agent = writer, target span =
this span. The writer's client library acks, the span transitions to
`OK`, the red outline clears, the pulse stops.

![TODO: screenshot of the drawer Control tab with Approve button before click](_screenshots/example-escalation-approve.png)

The attention badge on the app bar ticks down. The session row's `needs
attention` chip disappears.

### t=75s — writer resumes, then completes

The writer's INVOCATION continues — new LLM_CALL span, tool results,
another LLM_CALL. At t=~2 min the writer's INVOCATION closes and a
return arrow draws back to the coordinator.

### t=130s — coordinator closes out

Coordinator's final LLM_CALL formats the result. Its INVOCATION closes.
All three agent rows go idle (no open INVOCATIONs). The current task
strip sticks on the last completed task.

## What the UI looks like at the end

- Gantt: three rows, one long completed INVOCATION each, with the
  WAIT_FOR_HUMAN span rendered in its OK (approved) color in the
  middle of the writer's row.
- Graph view: three columns with three arrows (coordinator → researcher,
  coordinator → writer, plus returns). No amber markers.
- Attention badge: 0.
- Drawer → Task → Plan revisions: one entry if the escalation produced
  a `agent_escalated` revision, zero otherwise.

## Patterns to notice

1. **Escalation is a status transition, not a control.** The agent
   chose to block; you respond. If you want to force-stop instead, use
   `CANCEL`.
2. **Approve / Reject only show up on `AWAITING_HUMAN` spans.** If you
   miss the window (the writer times out and fails itself), the
   buttons are gone.
3. **The payload is the question.** Always check the Payload tab
   before approving — the agent put the context there for a reason.
4. **Graph view is the right surface for multi-agent runs.** The
   Gantt is denser but the Graph makes "who is talking to whom" obvious.
   See [graph-view.md](../graph-view.md).
5. **You can also `Reject`** — it sends with a default rejection reason
   string. The span transitions to `CANCELLED`/`ERROR` and the writer
   takes a different branch (or itself fails).

## Related

- [cookbook.md → watch a sub-agent escalate to human review](../cookbook.md#1-watch-a-sub-agent-escalate-to-human-review)
- [control-actions.md → approve / reject](../control-actions.md#approve--reject)
- [faq.md → what does awaiting human mean](../faq.md#what-does-awaiting_human-mean)
- [annotations.md → the three kinds](../annotations.md#the-three-kinds) — note the `HUMAN_RESPONSE` annotation kind is a different path.
