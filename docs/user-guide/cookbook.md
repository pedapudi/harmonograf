# Cookbook

Goal-oriented recipes. Each recipe names what you are trying to do, lists the
prerequisites, walks the exact UI interactions, tells you what you should see
at each step, and ends with a troubleshooting block for the common ways the
recipe goes sideways.

If you have not read [index.md](index.md) for the orientation map of regions,
skim it first — every recipe refers to regions by name.

## Recipes

1. [Watch a sub-agent escalate to human review](#1-watch-a-sub-agent-escalate-to-human-review)
2. [Replay a session after a crash](#2-replay-a-session-after-a-crash)
3. [Steer an agent mid-run without cancelling it](#3-steer-an-agent-mid-run-without-cancelling-it)
4. [Compare two plan revisions](#4-compare-two-plan-revisions)
5. [Find the bottleneck in a multi-agent pipeline](#5-find-the-bottleneck-in-a-multi-agent-pipeline)
6. [Export a session for an offline write-up](#6-export-a-session-for-an-offline-write-up)
7. [Filter and triage by drift kind](#7-filter-and-triage-by-drift-kind)
8. [Trace a transfer across agent columns](#8-trace-a-transfer-across-agent-columns)
9. [Pause one agent without freezing the whole session](#9-pause-one-agent-without-freezing-the-whole-session)
10. [Confirm an agent is still alive when the UI looks frozen](#10-confirm-an-agent-is-still-alive-when-the-ui-looks-frozen)

---

## 1. Watch a sub-agent escalate to human review

**Goal.** Catch the moment a sub-agent blocks on a human decision, inspect
what it's asking, and approve or reject it from the UI.

**Prerequisites.**

- A session with at least one agent that has built-in
  `WAIT_FOR_HUMAN` span support. ADK agents with the harmonograf client's
  `agent_escalated` drift kind wired up qualify. See
  [tasks-and-plans.md → drift kinds](tasks-and-plans.md#drift-kinds) for the
  full list.
- The agent has advertised it accepts the span-level approve/reject path
  (always available — approve/reject is not capability-gated; see
  [control-actions.md → capability negotiation](control-actions.md#capability-negotiation)).

**Steps.**

1. Open the session picker with `⌘K` (or `Ctrl+K`). If the offending session
   is already live, its row will show a red `N need attention` chip on the
   right. Click the row to open it.
2. The app bar's **bell** icon shows the aggregate attention count across
   every session. Hovering it will not open a drawer — the bell is a
   read-only aggregate today; see [sessions.md → attention badges](sessions.md#attention-badges).
3. On the Gantt, look for a bar with a **red outline and a 1 s pulse**. The
   full signature in [gantt-view.md → reading a bar](gantt-view.md#reading-a-bar--kinds-status-decorations)
   is: error-container fill + red outline + pulse, kind `WAIT_FOR_HUMAN`
   (the `⏸` glyph at widths ≥ 12 px).
4. Click the bar. The [drawer](drawer.md) opens on it.
5. Switch to the drawer's **Summary** tab to see `status = AWAITING_HUMAN`
   and the agent's escalation attributes (whatever the client stamped on the
   span — typically a `task_report` or `agent_note`).
6. Open the **Payload** tab if the agent attached the request body. Click
   **Load full payload** — payloads are not fetched eagerly (see
   [drawer.md → payload tab](drawer.md#payload-tab)).
7. Switch to the **Control** tab. Because the span is `AWAITING_HUMAN`,
   `Approve` and `Reject` are visible (they are only rendered for that
   status; see [control-actions.md → approve / reject](control-actions.md#approve--reject)).
8. Click **Approve** (empty payload) or **Reject** (the frontend sends the
   default rejection reason). The drawer's inline error line will surface
   any client-side error; otherwise the span transitions to `OK` (approve)
   or `CANCELLED`/`ERROR` (reject) and the red outline disappears.

**What to expect.** The escalation bar's color and outline switch immediately.
If the client correlated the `APPROVE`/`REJECT` control back to the
`WAIT_FOR_HUMAN` span, you will also see a new progress signal on the agent's
row — whatever the agent does next now that it's unblocked.

**Troubleshooting.**

- *Approve/Reject buttons are missing from the Control tab.* The span is not
  in `AWAITING_HUMAN`. Re-check the Summary tab — if status is already `OK`,
  you are too late (another operator or the agent itself timed out).
- *Approve button sends but nothing visibly happens.* Agents must ack the
  control for the visible transition; the frontend is optimistic about the
  UI state. A red line under the button is your only hint that the control
  was rejected client-side — see
  [control-actions.md → capability negotiation](control-actions.md#capability-negotiation).
- *The attention badge is lying.* See
  [troubleshooting.md → attention badge is wrong](troubleshooting.md#attention-badge-is-wrong).

---

## 2. Replay a session after a crash

**Goal.** The server restarted, the agent crashed, or you closed the
browser. Pick the session back up and confirm the timeline survived.

**Prerequisites.** The session's spans, annotations, and task plans were
persisted to the server's backing store before the crash. Harmonograf's
server keeps state in SQLite by default — sessions survive server restarts;
see the ops guide for tuning. The client buffered whatever it had locally
during the outage.

**Steps.**

1. Open the picker (`⌘K`). Sessions are bucketed into Live, Recent (≤24 h),
   Archive — see [sessions.md → live / recent / archive](sessions.md#live--recent--archive--how-a-session-is-bucketed).
2. If the session is still live (at least one stream reattached), it is
   under **Live** with a pulsing dot. Otherwise it is under **Recent** or
   **Archive**. Substring-search by title or id if you have many sessions.
3. Click the row. The Gantt loads whatever spans were durable on the server.
4. Look at the **agent gutter**: any agent with `status = crashed` renders
   with a red dot in the gutter. The [Graph view](graph-view.md) surfaces
   the same state on the agent header (red dot, red border).
5. If the client reconnected after the crash, it will replay its buffered
   spans through `SpanStart`/`SpanUpdate`/`SpanEnd`. The server dedups on
   `span.id` (see `docs/protocol/data-model.md :: Span.id`) so you will not
   see duplicates.
6. Scroll to the gap in the timeline where the crash happened. Spans that
   were `RUNNING` at the time are typically either:
   - Transitioned to `FAILED` by the server's liveness timeout (watch the
     `agent_escalated` / `failed_span` drift kind to tell the difference),
     or
   - Still `RUNNING` indefinitely if the server has not decided to time
     them out (the renderer draws them as breathing bars).

**What to expect.** The Gantt should render the full history up to the
crash. The **transport bar** elapsed clock resets to 0 when you pick a
session — this is a viewport convenience, not a replay control; the
underlying spans retain their real timestamps.

**Troubleshooting.**

- *The picker shows "Waiting for agents to connect…".* You have zero
  sessions on the server. If you expected one, the server restarted with a
  different database path — check the ops guide.
- *The picker shows "Server unreachable — showing demo sessions."* The
  frontend could not reach the server. You are looking at the baked-in
  demo, nothing on the screen is real. See
  [sessions.md → server unreachable](sessions.md#server-unreachable--the-mock-fallback).
- *Running bars hang forever.* The agent is gone and the server has not
  timed them out. Open the [Graph view](graph-view.md) and look for the
  amber "⚠ stuck" marker on the agent header — that is the liveness
  tracker saying the same thing.

---

## 3. Steer an agent mid-run without cancelling it

**Goal.** Tell a running agent to change direction, without stopping its
current invocation.

**Prerequisites.** The agent advertised the `STEERING` capability in its
`Hello`. (The frontend sends regardless — see
[control-actions.md → capability negotiation](control-actions.md#capability-negotiation)
— but without `STEERING` the ack is usually an error.)

**Steps.**

1. Find the running invocation on the Gantt. It is the breathing bar at
   the live edge on the target agent's row.
2. **Hover** the bar — the [span popover](drawer.md#span-popover) pops up.
3. Click **Steer** in the action row. The inline steer editor opens.
4. Pick a mode:
   - **⚡ Cancel & redirect** — the frontend sends a `STEER` control with
     JSON payload `{mode:"cancel", text}`. The client is expected to drop
     the in-flight turn and pick up your text as the new direction.
   - **+ Add to queue** — payload `{mode:"append", text}`. The client
     queues your text for the next model boundary without interrupting.
5. Type the steer text. Send with `⌘↵` / `Ctrl+↵`. `Esc` closes the editor.
6. Wait for the agent's next LLM call bar to appear. If your client honors
   the steer, the new model turn will show the steer text in its prompt
   payload — open the drawer's [Payload tab](drawer.md#payload-tab) on that
   LLM bar and click **Load full payload** to verify.

**What to expect.**

- A `user_steer` drift kind often fires soon after, producing a new plan
  revision. You will see it as a blue `👆 User steered` pill in the
  [PlanRevisionBanner](tasks-and-plans.md#planrevisionbanner) and as a new
  entry in the drawer's **Plan revisions** section. Not all clients attribute
  the drift — see [tasks-and-plans.md → drift kinds](tasks-and-plans.md#drift-kinds).
- The popover is **pin-able** (📌 in its top-right). If you want to steer
  multiple agents in a burst, pin the popover and open another span.

**Troubleshooting.**

- *Steer button does nothing when pressed.* The handler requires you to
  pick a mode first; the Send button is disabled until then. See
  [control-actions.md → confirmation policy](control-actions.md#confirmation-policy).
- *The `s` shortcut does not steer.* The `s` key is a stub pending
  task #14. Use the popover or drawer Control tab — see
  [keyboard-shortcuts.md](keyboard-shortcuts.md).
- *Steer control/annotation — which do I want?* See
  [annotations.md → steering from the popover](annotations.md#steering-from-the-popover-is-a-control-not-an-annotation).
  Control = one-shot, annotation = durable on the span.

---

## 4. Compare two plan revisions

**Goal.** The plan was revised mid-run. You want to see what actually
changed, not just notice the pill.

**Prerequisites.** The session had at least one plan revision. Every
revision produces a snapshot in the `TaskRegistry` and a `PlanDiff` — see
[tasks-and-plans.md → plan revisions](tasks-and-plans.md#plan-revisions--live-replans).

**Steps.**

1. On the Gantt, click any span that belongs to the plan you care about.
   The [drawer](drawer.md) opens.
2. Switch to the **Task** tab.
3. Scroll to the **Plan revisions** section. The latest revision is
   expanded by default; older revisions collapse. Revisions are listed
   newest-first with a drift-kind icon, label, category badge, relative
   timestamp, and diff counts `+N -M ~K`. A `⇄` marker means the DAG edges
   changed.
4. Click an older revision to expand it. Each expanded row shows:
   - Added / removed task chips.
   - Modified tasks with change descriptions.
   - A note when the edges (dependency DAG) changed.
5. To see the diff counts at a glance over the whole history, read the
   expanded rows top to bottom. The counts are cumulative per revision
   (computed by `TaskRegistry.upsertPlan` — see
   [tasks-and-plans.md → plan revisions](tasks-and-plans.md#plan-revisions--live-replans)).

**What to expect.** The same banner pill you saw transiently is here as a
durable record. If you missed the pill (it auto-dismisses after ~4 s), the
Plan revisions section is authoritative.

**Troubleshooting.**

- *I don't see my expected revision in the list.* See
  [troubleshooting.md → drift not firing](troubleshooting.md#drift-not-firing--plan-revision-banner-not-appearing).
  The planner may not have recognized the signal, or the drift kind is
  unknown to the frontend and fell back to `Plan revised`.
- *The drawer is empty.* You selected a span that is not task-bound; the
  Task tab falls back to the current RUNNING plan. To pin the drawer to a
  specific plan, click a span whose `hgraf.task_id` attribute points at
  that plan's task — see [drawer.md → task tab](drawer.md#task-tab) and
  `docs/protocol/data-model.md :: Span.attributes`.

---

## 5. Find the bottleneck in a multi-agent pipeline

**Goal.** Five agents running, the session feels slow, you want to know
which one is holding the rest up.

**Prerequisites.** At least two agents with real cross-agent traffic —
TRANSFER spans or delegated INVOCATION parents.

**Steps.**

1. Open the session. On the Gantt, press `f` to fit the full time range
   into the viewport (see [keyboard-shortcuts.md](keyboard-shortcuts.md)).
2. Scan the rows for the agent whose bars are **widest** or **running**
   longest. INVOCATION spans render recessed — look at their full extent,
   not just the bar. Streaming LLM bars have ticks on their trailing edge,
   which is a signal the agent is at least alive.
3. Switch to the [Graph view](graph-view.md) (◈ in the nav rail).
4. Read the header row: `N transfers · M delegations · K returns`. If
   delegations heavily outnumber returns, some delegated agents have not
   finished yet — they are the likely bottleneck.
5. Look for **amber "⚠ stuck"** markers on agent headers. The liveness
   tracker flags an agent when its open INVOCATION has made no recent
   progress — that is the honest "we think this agent is wedged" state.
   See [troubleshooting.md → plan stuck](troubleshooting.md#plan-stuck--not-progressing).
6. For every amber agent, click `↻ Status` in the agent header. This
   sends a `STATUS_QUERY` control and surfaces the agent's own "what am I
   doing right now" reply as the task report line on the header. Timeout
   is 8 s — see [control-actions.md → status query](control-actions.md#status-query).
7. For a numeric comparison, open the drawer on each agent's most recent
   INVOCATION span and read the **Duration** line from the Summary tab.
   Completed invocations are reported via `formatDuration(ms)`; running
   ones read `running`.
8. If the bottleneck is not a single agent but a cross-agent wait, follow
   the **Links** tab on the slow span. `WAITING_ON` links list the spans
   this one is blocked behind — that edge is the bottleneck.

**What to expect.** Typically one of: a single agent stuck in an LLM call
(fix with a steer or cancel); a delegation chain where an inner agent never
returns (fix by cancelling the inner agent); or tool calls serialized
behind a slow external API (fix outside harmonograf).

**Troubleshooting.**

- *Everything looks fast but the user says it is slow.* Widen the time
  window. The Gantt's minimap shows the full session and the viewport
  rectangle — check whether you are zoomed into a fast stretch.
- *No agent has an amber border, but the session looks frozen.* The
  liveness tracker only flags open INVOCATIONs. If the agents are waiting
  on a user message (`USER_MESSAGE` span kind), that is a legitimate
  idle state, not a stuck one.

---

## 6. Export a session for an offline write-up

**Goal.** Save what you are looking at so you can share it or write it up
without the live session.

**Prerequisites.** None — this recipe is entirely about working within the
current limitations.

**Status.** Harmonograf has **no first-class export button** in the
frontend today. The session picker is read-only; there is no
delete/replay/export UI (see [sessions.md → deleting / replaying sessions](sessions.md#deleting--replaying-sessions)).
What you can do:

**Steps.**

1. **Take screenshots** of the key moments. Every shareable surface has
   a stable layout: Gantt, Graph, drawer tabs, plan revisions section.
   Use the browser's screenshot tool; the frontend has no built-in
   screenshotter.
2. **Copy span ids** from the span popover's **Copy id** action (see
   [control-actions.md → span popover](control-actions.md#3-span-popover-quick-look)).
   Paste them into your write-up so future readers can deep-link back.
   Deep links use `?span=<id>` — see
   [troubleshooting.md → drawer is blank](troubleshooting.md#drawer-is-blank--shows-select-a-span).
3. **Pin popovers** (📌 in the popover header) to keep multiple span
   summaries visible side-by-side while you screenshot. Pinned popovers
   stack and do not auto-dismiss. See
   [control-actions.md → span popover](control-actions.md#3-span-popover-quick-look).
4. **Copy payload text** from the drawer's Payload tab once you have
   clicked **Load full payload**. JSON payloads pretty-print; text is
   plain `<pre>`; images render inline (right-click → copy image).
   Evicted payloads cannot be recovered — see
   [drawer.md → payload tab](drawer.md#payload-tab).
5. **Use annotations for in-session narration.** Post `COMMENT`
   annotations from the drawer's Annotations tab on the spans that
   matter (see [annotations.md](annotations.md)). They persist
   server-side and show as pins over the Gantt on reload, so your
   narrative survives the session ending.
6. For programmatic export, read the server's `ListSessions` /
   `WatchSession` RPCs directly (`docs/protocol/frontend-rpcs.md`).
   The frontend does not layer anything between the wire and the UI
   beyond what is documented there.

**What to expect.** A collection of screenshots plus the annotations you
posted while watching. Not a tarball.

**Troubleshooting.**

- *I want a proper export and I want it now.* File an issue. This is a
  known gap, not a hidden feature.

---

## 7. Filter and triage by drift kind

**Goal.** You know something broke, and you want to see only the
`tool_error` revisions (or the `llm_refused`, or the `context_pressure`…).

**Prerequisites.** The session has had at least one drift-tagged
revision. See [tasks-and-plans.md → drift kinds](tasks-and-plans.md#drift-kinds)
for the full taxonomy.

**Steps.**

1. Open the drawer on any span belonging to the plan you care about (any
   task-bound span will do; see [drawer.md → task tab](drawer.md#task-tab)).
2. Switch to the **Task** tab.
3. Scroll to **Orchestration events** (embedded `OrchestrationTimeline` at
   the bottom of the tab). It shows the last 20 orchestration events for
   the session: start / progress / complete / fail / block / discovered
   / divergence.
4. Use the filter controls at the top of the component to restrict by:
   - **Kind** (matches the orchestration event kind, not the drift
     taxonomy exactly — the drift kind is stamped as `drift_kind` on the
     active INVOCATION span).
   - **Agent**.
   - **Time window**.
   - **Collapse noise** toggle — de-duplicates repeated progress ticks.
5. For the full drift history (not just the last 20), read the **Plan
   revisions** section above the events timeline. Every revision includes
   its drift kind icon and label; scroll through visually to triage.
6. To triage at the session level instead of per-plan, switch to the
   **Notes** section in the nav rail (the ✎ icon) — it aggregates every
   annotation across the session. Combine with `COMMENT` annotations
   tagged by you to build a ragged-but-durable filter. See
   [annotations.md → where annotations show up](annotations.md#where-annotations-show-up).

**What to expect.** A scoped event timeline plus a complete revision
history, both in one drawer. There is no session-wide "show only drift
kind X" filter today.

**Troubleshooting.**

- *The drift kind I want is not in the icon table.* Unknown kinds fall
  back to `Plan revised` with a grey icon; the raw reason is still in the
  pill body. See [troubleshooting.md → the drift kind may be unknown](troubleshooting.md#the-drift-kind-may-be-unknown-to-the-frontend).
  Add the kind to `frontend/src/gantt/driftKinds.ts` if you want a real
  label.
- *The orchestration events timeline is empty.* The client may not be
  emitting orchestration events. The drift kind table still drives the
  plan revision banner; inspect there instead.

---

## 8. Trace a transfer across agent columns

**Goal.** Follow a delegation chain visually from the originating agent
through to the return.

**Prerequisites.** A session with at least two agents and at least one
explicit `TRANSFER` span (or an inferred delegation — both work).

**Steps.**

1. Switch to the [Graph view](graph-view.md) (◈ in the nav rail). This is
   the flow-optimized surface; the Gantt shows the same edges as bezier
   curves but the Graph is cleaner for walking chains.
2. Find the source agent's column. Walk down its lifeline until the first
   **orange solid 2.5 px arrow** (transfer). The label above the arrow is
   the transfer span's name, truncated to ~22 characters.
3. The arrow head lands on the top of the destination's activation box.
   Click the destination box to open the drawer on that invocation.
4. In the drawer, switch to the **Links** tab. `INVOKED` shows what this
   invocation started; `TRIGGERED_BY` is the inverse (back to the
   transfer span you just walked from). See
   [drawer.md → links tab](drawer.md#links-tab).
5. Walk forward by clicking any row in the Links list — the drawer
   re-selects on that target span and scrolls the Gantt / Graph to it.
6. Continue until the invocation ends. Look for a **grey dashed return
   arrow** heading back to the previous column. If it is missing, the
   return could not be computed — common in ADK's observer mode (see
   [graph-view.md → following message flow](graph-view.md#following-message-flow)).

**What to expect.** A visual chain you can read like a sequence diagram,
with clickable handoff points.

**Troubleshooting.**

- *Arrow is dashed blue, not orange.* That is a delegation (inferred from
  a cross-agent INVOCATION parent) not an explicit TRANSFER. The chain is
  still valid; you just do not have a TRANSFER span to inspect.
- *No return arrow.* Either the invocation is still running, or the
  return could not be inferred. See [graph-view.md → arrows](graph-view.md#arrows--transfer-delegation-return).

---

## 9. Pause one agent without freezing the whole session

**Goal.** You want to stop **one** agent (maybe to investigate its state)
while the rest keep running.

**Prerequisites.** The agent advertised `PAUSE_RESUME` in its capability
set. See [control-actions.md → capability negotiation](control-actions.md#capability-negotiation).

**Steps.**

1. On the Gantt, click any span belonging to the target agent. The
   [drawer](drawer.md) opens.
2. Switch to the **Control** tab.
3. Click **Pause agent**. This sends a `PAUSE` control scoped to this
   span's agent id (not the whole session).
4. The agent stops accepting new turns at its next safe boundary;
   in-flight LLM and tool calls finish on their own.
5. To resume, click **Resume** on the same tab.

**What to expect.** Only the paused agent's bars stop extending. The
transport bar at the bottom of the Gantt does **not** switch to `⏸
AGENTS PAUSED` — that label is for the session-wide pause via the
transport bar, which sends a `PAUSE` to every agent in the session (see
[control-actions.md → transport bar](control-actions.md#1-transport-bar-session-wide)).

**Troubleshooting.**

- *Do not use the transport bar pause by mistake.* The transport bar
  pauses the **whole session**. If you wanted one agent, undo by
  clicking **Resume** immediately — it also broadcasts.
- *Button sends but agent keeps working.* The agent did not advertise
  `PAUSE_RESUME`, or the client ignores the control. The drawer's inline
  error line is the signal. See
  [control-actions.md → capability negotiation](control-actions.md#capability-negotiation).

---

## 10. Confirm an agent is still alive when the UI looks frozen

**Goal.** The Gantt is not moving. You want to rule out "frontend stuck"
from "agent wedged" from "nothing happening".

**Steps.**

1. Check the **transport bar**. If it reads `⏸ AGENTS PAUSED` you
   paused the session yourself. Click **▶ Resume**. If it reads `○
   Viewport locked`, the viewport has drifted — press `L` or click **↩
   Follow live** to re-attach. Neither of those is an agent problem.
2. Switch to the [Graph view](graph-view.md) and look for amber **⚠
   stuck** markers on agent headers. An amber border means the liveness
   tracker flagged the agent — an open INVOCATION with no recent progress
   signal. See [troubleshooting.md → plan stuck](troubleshooting.md#plan-stuck--not-progressing).
3. Click the amber agent's `↻ Status` button. This sends a
   `STATUS_QUERY` control. Within 8 s the agent's task report line
   should update with whatever the agent says it is doing.
4. If the status query returns a valid detail, the agent is alive and
   currently self-reporting — whatever looked frozen is probably
   expected idle (long LLM call, waiting on a tool, etc.).
5. If the status query times out (the button stops spinning with no
   update), the agent process is likely hung. Now you need to look at
   process-side logs; harmonograf has told you everything it can.
6. If **no** agent is amber but bars still are not moving, check the
   current task strip's **thinking dot** (see
   [tasks-and-plans.md → currenttaskstrip](tasks-and-plans.md#currenttaskstrip)).
   A pulsing dot + no new bars is a long reasoning step, which is normal.

**What to expect.** A short yes/no on whether the agent is alive, plus a
fresh task report line on the agent header.

**Troubleshooting.**

- *Status button returns empty.* `sendStatusQuery` returns `''` on timeout
  or error. The button just stops spinning. Assume process-side problem.
- *All agents are amber.* Either the server's liveness thresholds are too
  aggressive, or everything really is stuck. Test with a `STATUS_QUERY`
  against each before escalating.

---

## Related pages

- [gantt-view.md](gantt-view.md) — the main visual surface.
- [graph-view.md](graph-view.md) — flow-oriented alternative.
- [drawer.md](drawer.md) — the deep inspector every recipe drops into.
- [control-actions.md](control-actions.md) — complete control reference.
- [tasks-and-plans.md](tasks-and-plans.md) — plan revisions and drift kinds.
- [troubleshooting.md](troubleshooting.md) — diagnostic tree when a recipe fails.
- [faq.md](faq.md) — one-liner answers to adjacent questions.
- [glossary.md](glossary.md) — terminology reference.
