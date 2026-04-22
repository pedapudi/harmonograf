# FAQ

Short answers to the questions people actually ask once they start using
harmonograf. Links throughout point at the deeper reference material.

If the question you have is not here, the [troubleshooting](troubleshooting.md)
page is organized by symptom and may cover it. For terminology, see
[glossary.md](glossary.md).

## Sessions, agents, connections

### Why doesn't my agent show up in the session picker?

Either the server cannot see it, or you cannot see the server. Check both:

1. Does the picker show `Server unreachable — showing demo sessions.` under
   the search field? The frontend fell back to baked-in mock data; nothing
   on-screen is real. See
   [sessions.md → server unreachable](sessions.md#server-unreachable--the-mock-fallback).
2. Is the agent actually calling `Hello` on startup? No `Hello`, no session
   row. The agent's stderr will show a traceback if the
   `harmonograf_client` import crashed on load.
3. Does the session exist but show `0 agents`? The client connected at some
   point but has no live telemetry stream now. Check process logs; the
   agent may have crashed mid-run.

See [troubleshooting.md → agents aren't showing up](troubleshooting.md#agents-arent-showing-up).

### Why are there multiple streams for one agent?

Harmonograf allows **multiple concurrent telemetry streams under one
`agent.id`**. The agent id is client-chosen and persisted to disk so a
restarted agent reclaims its gutter row, but each physical stream gets its
own `stream_id` from `Welcome`. This is the normal path when an agent
forks worker processes that all share one identity. See
[`docs/protocol/data-model.md`](../protocol/data-model.md) :: Agent.id.

### Why does one `goldfive.wrap` run show N agent rows now?

As of harmonograf#80, a single run drives a tree of ADK agents (a
coordinator, its specialist sub-agents, AgentTool wrappers, workflow
containers) and each renders on its own Gantt row with a derived id
of the form `<client.agent_id>:<adk_agent_name>`. Old sessions
recorded pre-#80 still collapse onto the client root — that's
expected, they predate the fix. See
[gantt-view.md → per-agent rows](gantt-view.md#per-agent-rows-80).

### Why is there only one session per run now? (No more ghost sessions?)

Lazy Hello (#84 / #85) deferred the `Hello` RPC until the client's
first emitted envelope. Importing `harmonograf_client` in a launcher
or interactive notebook no longer mints an empty session; the picker
only shows sessions that actually had activity.

One session per end-to-end `goldfive.wrap` call is now the contract —
AgentTool sub-Runners inside the run land on the same session id as
the root via the plugin's root-session rollup. Expected; not a bug.

### Why does my session say `0 agents`?

The session was created (typically via a stale `ListSessions` row) but no
client stream has a live `Hello`. Most common cause: the agent process
crashed during startup before it could call `Hello`. Check the stderr of
the agent process; an import-time failure in `harmonograf_client` prevents
any connection at all.

### Why does the picker show demo sessions I never created?

You are in the mock fallback. Something between your browser and the
harmonograf server is broken (server not running, wrong URL, TLS
mismatch, firewall). See [troubleshooting.md → is the server reachable](troubleshooting.md#is-the-server-reachable).

### Can I delete or archive a session from the UI?

No. The picker is read-only in the current release (see
[sessions.md → deleting / replaying sessions](sessions.md#deleting--replaying-sessions)).
Session lifecycle is driven server-side. The **Archive** bucket is just a
classification (older than 24 h), not a deletable state.

### How do I switch sessions quickly?

Press `⌘K` (Mac) or `Ctrl+K` (Linux/Windows). `/` is an alias. Or click the
session title in the app bar (the button with the ▾ marker). See
[sessions.md → opening the picker](sessions.md#opening-the-picker).

## Tasks, plans, drift

### Why is my plan stuck?

The current task strip reads a RUNNING task that has not changed for a
while. Two likely causes:

1. **Agent is wedged.** Open the [Graph view](graph-view.md) and check for
   an amber **⚠ stuck** marker on the agent header. That is the
   server-side liveness tracker's official "no recent progress" flag.
2. **Task state machine is correct, reality is slow.** A long LLM call or
   external tool can hold a task in `RUNNING` legitimately for minutes.
   The [thinking dot](tasks-and-plans.md#currenttaskstrip) on the current
   task strip (pulsing blue when the assignee has `has_thinking=true`) is
   your hint that the agent is actually reasoning, not hung.

Next step: click `↻ Status` on the agent header in the Graph view to send
a `STATUS_QUERY` and read the agent's own self-report. See
[troubleshooting.md → plan stuck](troubleshooting.md#plan-stuck--not-progressing).

### Why did the plan get revised mid-run?

Some drift signal fired. Open the drawer on any task-bound span in that
plan and scroll to the **Plan revisions** section of the Task tab. Each
revision shows its drift kind, category, and a detailed diff. The
[PlanRevisionBanner](tasks-and-plans.md#planrevisionbanner) you just
saw is the transient version of the same data.

Common drift kinds: `TOOL_ERROR`, `USER_STEER`, `PLAN_DIVERGENCE`,
`CONFABULATION_RISK`, `LOOPING_REASONING`, `AGENT_REFUSAL`,
`HUMAN_INTERVENTION_REQUIRED`, `GOAL_DRIFT`. See
[tasks-and-plans.md → drift kinds](tasks-and-plans.md#drift-kinds) for the
complete table.

### How do I know what drift kind fired?

Look in either place:

- The **PlanRevisionBanner** pill immediately after the revision lands
  (auto-dismisses after ~4 s).
- The **Plan revisions** section in the drawer's Task tab (durable). Every
  revision there has its drift icon, label, category badge, and raw
  reason detail.

If the icon is a generic grey `Plan revised`, the planner emitted a drift
kind that is not in `frontend/src/gantt/driftKinds.ts` yet. The raw reason
text is still on the pill. See
[troubleshooting.md → the drift kind may be unknown](troubleshooting.md#the-drift-kind-may-be-unknown-to-the-frontend).

### Why doesn't the plan revision banner appear when I expected?

Three candidates:

1. Drift detection in the client library is a heuristic; it can miss
   signals. A tool that swallowed its own error and returned success will
   not produce a `tool_error` drift kind.
2. The pill auto-dismisses after ~4 s. If you blinked, you missed it.
   Open the drawer's Plan revisions section for the durable record.
3. The banner dedupes on `revisionReason` — identical reasons show once
   then stay quiet. See
   [troubleshooting.md → revisions are coming in](troubleshooting.md#revisions-are-coming-in-but-the-banner-stays-empty).

### What does `AWAITING_HUMAN` mean?

A span's status transitions to `AWAITING_HUMAN` when the agent has
explicitly blocked on a human decision — typically a `WAIT_FOR_HUMAN`
span, though any span can adopt this status if the client updates it.
The bar renders with a red outline and a 1 s pulse; the drawer's Control
tab exposes `Approve` / `Reject` buttons while the status holds. See
[control-actions.md → approve / reject](control-actions.md#approve--reject).

### What's the difference between pausing an agent and pausing the session?

- **Transport bar pause** → sends `PAUSE` to **every agent in the
  session**, plus freezes the renderer locally so bars stop extending.
  Status switches from `LIVE` to `⏸ AGENTS PAUSED`. See
  [control-actions.md → transport bar](control-actions.md#1-transport-bar-session-wide).
- **Drawer Control tab pause** → sends `PAUSE` to the **single agent**
  that owns the selected span. Session status does not change.

### How do I see what the agent is thinking?

Click a running INVOCATION or LLM span on the Gantt. In the drawer's
**Task** tab, look for the **Model thinking** block. It prefers
`llm.thought` (the goldfive-side aggregate), falls back to
`thinking_text`, then `thinking_preview`. The label says `(live)` while
the span is running and `has_thinking=true`. See
[drawer.md → task tab](drawer.md#task-tab).

If the block is empty, the client did not capture thinking for that span.
Check your client configuration — some clients default thinking capture
off for LLM_CALL spans.

### How do I know the context window is full?

The Gantt renders a per-agent context-window overlay at the bottom of
each agent row — see
[gantt-view.md → context window overlay](gantt-view.md#context-window-overlay).
The ratio also lands on the per-agent header chip for an at-a-glance
read. For programmatic detection, goldfive's per-LLM-call metrics
(goldfive#172) expose `goldfive.llm.request.chars` and
`goldfive.llm.usage.*_tokens` — watch those approach `limit_tokens`.

See [runbooks/context-window-exceeded.md](../../runbooks/context-window-exceeded.md)
for the full diagnosis flow.

## Interventions

### Why does a single STEER show up as one card, not three?

The intervention aggregator (server `interventions.py` + frontend
`lib/interventions.ts`) deduplicates by `annotation_id`. A user STEER
emits three wire events — the annotation itself, a `USER_STEER` drift
goldfive mints from it, and a `PlanRevised` that follows once the
planner refines — and the aggregator collapses all three onto the
annotation row. See
[trajectory-view.md → intervention cards dedup by annotation_id](trajectory-view.md#intervention-cards-dedup-by-annotation_id).

### Why does my STEER take 30-70 seconds to "apply"?

The steer is acknowledged by the agent within milliseconds; what
takes 30-70 s is the *planner's refine call*. Goldfive's steerer
observes the USER_STEER drift, calls its planner LLM with the full
task state + the steer body, and emits `PlanRevised` only when the
LLM returns. A local Qwen3.5-35B routinely takes 20-60 s for this.

The intervention card's outcome chip flips from `pending` to
`→ rev N` as soon as the revision lands, which can be a minute or two
later. See
[control-actions.md → why STEER sometimes takes 30-70 seconds to apply](control-actions.md#why-steer-sometimes-takes-30-70-seconds-to-apply).

### Why does a completed session's Gantt open past the last span?

Known UX rough edge tracked as harmonograf#89: the viewport opens to
a window past the final span on completed sessions, and the LIVE
badge may briefly show. Press **F** to fit the whole session, or drag
the minimap to the spans region. A real fix is in flight.

## Orchestration

### What are the orchestration modes under the hood?

Goldfive exposes three executor modes (`SequentialExecutor`,
`ParallelDAGExecutor`, delegated/observer). Harmonograf renders them
as chips on the current task strip. With the overlay refactor
(goldfive#141-144) the per-task driving was replaced with an
observation-driven overlay: tasks are driven by agent activity rather
than pulled off a queue, and unassigned tasks transition from
`PENDING → NOT_NEEDED` when the invocation ends. See
[tasks-and-plans.md](tasks-and-plans.md).

Which mode a session is in is fixed at `goldfive.wrap` call time, not
toggleable from the UI.

### How do sub-agents know which task they are on in parallel mode?

The parallel executor sets a task-id ContextVar before invoking the
sub-agent; goldfive's `ADKAdapter` stamps it on agent traffic. The
harmonograf side reads it and writes `hgraf.task_id = <task.id>` on
every span opened during the sub-agent's run. Task terminal transitions
are enforced monotonic inside goldfive's `DefaultSteerer`, so the
walker cannot rewind a completed task. See
[../goldfive-integration.md](../goldfive-integration.md) for the
integration overview.

## Payloads

### Why are payloads missing?

Three flavors of "missing":

1. **"No payload attached to this span."** — the span never carried a
   payload. LLM_CALL spans without prompt/response capture,
   TOOL_CALL spans without result capture. The client library controls
   whether payloads are captured. See
   [drawer.md → payload tab](drawer.md#payload-tab).
2. **"Payload was not preserved (client under backpressure)."** — the
   client attached a ref but dropped the bytes under backpressure (memory
   pressure, too many in-flight uploads). The ref persists with
   `evicted: true`; the summary (if the client wrote one) is still
   visible. Remedy: increase client buffer, reduce capture volume, or
   accept the loss. See
   [troubleshooting.md → payloads are missing](troubleshooting.md#payloads-are-missing).
3. **Spinner hangs on "Load full payload".** The `getPayload` RPC
   stalled. Check the browser network tab.

### What does "payload digest" mean?

`sha256` of the payload bytes as hex. Identical bytes share one digest
across sessions; the server only frees the bytes when no refs remain. See
[`docs/protocol/data-model.md`](../protocol/data-model.md) :: PayloadRef.

## Controls and steering

### What's the difference between a steer control and a steering annotation?

Explained in [annotations.md → steering from the popover](annotations.md#steering-from-the-popover-is-a-control-not-an-annotation):

- **STEER control** — one-shot. Goes through the control channel, is
  acked, and disappears. Use when you want to course-correct now.
- **STEERING annotation** — durable. Stored on the span, visible in the
  drawer later, can be re-read by the client on subsequent turns.

### Why does the `s` / `a` shortcut do nothing?

Both are **stubs** in `frontend/src/lib/shortcuts.ts` and not yet wired.
Use the span popover's Steer / Annotate actions, or the drawer's
Control / Annotations tabs. See
[keyboard-shortcuts.md](keyboard-shortcuts.md).

### Why doesn't `←` / `→` pan the Gantt?

Same reason. The arrow-key pan handlers are reserved in `shortcuts.ts`
but their behavior is pending renderer wiring. Use the minimap (click or
drag) or the `+` / `-` zoom buttons, or scroll-wheel pan when the cursor
is in the plot.

### Can I undo a cancel?

No. There is no undo for any control action. `CANCEL` in particular is
destructive — the in-flight invocation transitions to `CANCELLED` and any
in-flight tool calls abort if the client supports it. Harmonograf does
not prompt for confirmation on controls (see
[control-actions.md → confirmation policy](control-actions.md#confirmation-policy)).
Think before clicking on a production run.

### Why is the Approve button greyed out / missing?

The `Approve` / `Reject` buttons only render on the Control tab when
`span.status == AWAITING_HUMAN`. If the status is anything else, the
buttons are simply not shown. See
[drawer.md → control tab](drawer.md#control-tab).

### Why doesn't the frontend grey out controls for capabilities the agent lacks?

Deliberate design choice. The frontend does **not** hide buttons for
controls an agent hasn't advertised, because capability sets are often
stale and hiding controls hides discovery. Send is best-effort, and any
rejection surfaces as a red inline error in the originating surface. See
[control-actions.md → capability negotiation](control-actions.md#capability-negotiation).

## UI state and persistence

### What happens if the server crashes?

Durable server state (spans, annotations, task plans) survives a
restart because the default backing store is SQLite. When the server
comes back, reconnecting clients replay their buffered spans via
`SpanStart` / `SpanUpdate` / `SpanEnd`, and the server dedups on
`span.id`. Sessions that were live are marked according to whether their
clients reconnect.

What does **not** survive: ephemeral viewport state in the browser
(you have to re-pick the session, re-pan, etc.). Local preferences like
theme, task plan mode, and task panel height persist in `localStorage`.

### What persists across reloads?

Per `localStorage`:

- Theme and color-vision mode (`frontend/src/theme/store.ts`).
- Task plan render mode on the Graph view (`harmonograf.taskPlanMode`).
- Task panel expanded height (`harmonograf.taskPanelHeight`).
- Hidden agent set from the Gantt gutter.

Private / incognito browsing drops these writes silently; the app
reverts to defaults on reload. This is expected — see
[troubleshooting.md → theme / color-vision mode isn't sticking](troubleshooting.md#theme--color-vision-mode-isnt-sticking).

### Why is my theme not sticking?

You are probably in a private browsing window. `localStorage` writes are
silently dropped; the app reverts on reload.

### Why did the drawer go blank after switching sessions?

The drawer is bound to a `selectedSpanId`; if you switch sessions, the id
may not exist in the new session's store and the drawer shows
`Select a span on the Gantt to inspect it.` Press `Esc` to clear the
selection, then click a span in the new session. See
[troubleshooting.md → drawer is blank](troubleshooting.md#drawer-is-blank--shows-select-a-span).

## Visual glyphs and colors

### What do the bar colors mean?

Hue encodes span kind (`INVOCATION`, `LLM_CALL`, `TOOL_CALL`, etc.). Fill
and outline encode status (running, completed, failed, cancelled,
replaced, awaiting human). Icons at widths ≥ 12 px encode kind again with
a glyph. The **Legend** modal (the `?` button on the app bar) is the
authoritative visual reference. See
[gantt-view.md → reading a bar](gantt-view.md#reading-a-bar--kinds-status-decorations).

### What are the ticks on the trailing edge of a running LLM bar?

Streaming ticks. Each one is a `streaming_tick` event the client
reported — a signal that tokens are arriving. If a running LLM bar has no
ticks for a long time, the model may be stalled. See
[gantt-view.md → reading a bar](gantt-view.md#reading-a-bar--kinds-status-decorations).

### What is the blue dot next to an agent's name?

A pulsing blue dot means that agent's current span has `has_thinking =
true` — the model is in a reasoning phase. The same signal feeds the
thinking dot on the current task strip. See
[tasks-and-plans.md → currenttaskstrip](tasks-and-plans.md#currenttaskstrip)
and [drawer.md → task tab](drawer.md#task-tab).

### Why is an agent row highlighted amber on the Graph view?

The liveness tracker flagged it. Amber border + halo + "⚠ stuck" label =
open INVOCATION with no recent progress signal. See
[graph-view.md → agent headers](graph-view.md#agent-headers).

## Deep inspection

### How do I walk from a transfer to the invoked child?

Click the transfer bar on the Gantt (or the transfer arrow on the Graph
view). Open the drawer's **Links** tab. The `INVOKED` relation row points
at the destination invocation — click it to re-select the drawer on the
target. See [drawer.md → links tab](drawer.md#links-tab) and
[graph-view.md → arrows](graph-view.md#arrows--transfer-delegation-return).

### How do I see every child of an invocation without clicking each one?

Open the drawer on the parent invocation and switch to the **Timeline**
tab. It lays every child span out as a horizontal waterfall relative to
the parent's time range. Running parents use `now` as the scaling end.
See [drawer.md → timeline tab](drawer.md#timeline-tab).

### The span popover keeps disappearing when I try to read it.

Click the 📌 pin icon in the popover's top-right. Pinned popovers stack
and don't auto-dismiss when another span is clicked. Useful for
side-by-side comparison or keeping a steer editor open. See
[control-actions.md → span popover](control-actions.md#3-span-popover-quick-look).

### How do I deep-link to a specific span?

The drawer reads `?span=<id>` from the URL on load. Copy the span id with
the **Copy id** action in the popover (or right-click menu) and paste
into a URL. If the span has not streamed in yet, the drawer shows
`Select a span on the Gantt to inspect it.` until it arrives. See
[troubleshooting.md → drawer is blank](troubleshooting.md#drawer-is-blank--shows-select-a-span).

## Annotations and notes

### Do annotations get sent to the agent?

Depends on the kind. `COMMENT` is UI-only — purely a note. `STEERING` is
routed as a synthesized `STEER` control and may be picked up by the
client on the next turn. `HUMAN_RESPONSE` is routed as an `APPROVE`
control targeting an `AWAITING_HUMAN` span. See
[annotations.md → the three kinds](annotations.md#the-three-kinds) and
[`docs/protocol/data-model.md`](../protocol/data-model.md) :: Annotation.

### Can I edit or delete an annotation?

No. Annotations are append-only in the current release. If you need to retract
something, post a follow-up annotation. See
[annotations.md → deleting / editing](annotations.md#deleting--editing).

### Who is listed as the author of my annotation?

The default author is `user`, set client-side. A deployment with auth
can override the author string server-side; the current release has no
user-identity system in the frontend itself. See
[annotations.md → who wrote what](annotations.md#who-wrote-what).

## Performance and scale

### The Gantt is laggy with thousands of spans — what do I do?

Zoom in. The renderer draws every span every frame; at very wide time
windows with many agents, you are paying for a lot of off-screen geometry.
Press `+` several times to narrow the visible range, or use the minimap
to seek rather than scroll.

### Why do some agents go unfollowed when I pan?

Pausing agents or any manual pan / minimap drag disables live-follow. The
LIVE badge on the transport bar switches to `○ Viewport locked`. Press
`L` or click **↩ Follow live** to re-attach. See
[gantt-view.md → live follow](gantt-view.md#live-follow).

### Why do my hidden agents still appear on the minimap?

Intentional. The minimap always shows every agent so you can see there
are rows you're hiding. Only the main plot filters them out. See
[gantt-view.md → focused and hidden agents](gantt-view.md#focused-and-hidden-agents).

## Odd behavior

### The attention badge shows a number but I can't find the session.

The bell aggregates `RpcSession.attentionCount` across every session in
`ListSessions`. If a span transitioned out of `AWAITING_HUMAN` but the
session row wasn't re-emitted, the bell can lag. Open and close the
picker to force a refresh. Still wrong? Server-side bug — file it. See
[troubleshooting.md → attention badge is wrong](troubleshooting.md#attention-badge-is-wrong).

### I pressed `⌘K` and nothing happened.

Very rare. Press `Esc` first — another overlay may be capturing keyboard
input (help overlay, legend, theme menu). `Esc` → `⌘K` usually clears a
stuck state. If the global handler is still unresponsive, a crashing
component has unmounted it — check the browser console.

### The elapsed clock on the transport bar reset itself.

Expected. The elapsed clock resets to zero when you pick a session. It is
a viewport convenience, not a wall-clock for the run. The session's real
timeline is in the span timestamps.

### The drawer's Control tab has a red line under the buttons.

An ack from the server or client rejected your control. The text of the
error is on that line. Common causes: the agent did not advertise the
capability; the span is not in a state that accepts the control; the
control channel is down.

## Related pages

- [cookbook.md](cookbook.md) — goal-oriented walkthroughs.
- [troubleshooting.md](troubleshooting.md) — symptom-keyed diagnostics.
- [glossary.md](glossary.md) — term definitions.
- [examples/](examples/) — narrative scenarios.
- `AGENTS.md` — protocol-level invariants of the plan execution protocol.
