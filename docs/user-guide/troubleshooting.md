# Troubleshooting

What to do when the UI is telling you something isn't right — or when
it's telling you nothing at all. Symptoms first, then causes, then
remedies. When you have to look at code, file paths are relative to the
repo root.

## Agents aren't showing up

**Symptom:** The session picker is empty (`Waiting for agents to
connect…`) or a picked session shows zero rows on the Gantt and Graph.

### Is the server reachable?

- If the picker shows a banner `Server unreachable — showing demo
  sessions.`, the frontend couldn't reach the harmonograf server. The
  picker has fallen back to baked-in mock sessions; nothing you do
  inside them is real.
- Confirm the server is running and the frontend's `NEXT_PUBLIC_*` (or
  equivalent) endpoint points at it.
- Check the browser devtools network tab for failing
  `ListSessions` / `WatchSession` requests.

### Is the agent actually connecting?

- Confirm the agent process is calling `Hello` on startup. No Hello,
  no session row.
- Check the agent's logs for handshake errors. Common causes:
  - Wrong server URL.
  - TLS mismatch (client expects TLS, server is plaintext, or vice versa).
  - Clock skew breaking auth tokens.
- The server's own logs will show a rejected `Hello` if authorization
  fails.

### Session exists, agent list is empty

- The session row in the picker shows "0 agents". The session was
  created but no client has connected.
- Typically this means the agent crashed during startup (the
  harmonograf_client is embedded, so an import error prevents any
  connection).
- Check the agent's stderr; a Python traceback from
  `harmonograf_client` will be visible.

## Gantt is empty

**Symptom:** You picked a session, the Graph view shows agents, but
the Gantt plot is a blank grid.

### Zoom issue

- The time window may be too wide for the spans. Press `f` to fit the
  session to the viewport. Alternatively press `+` a few times to zoom
  in.
- If spans are extremely brief, they may render below 1px wide and be
  invisible at the current zoom level.

### Live follow is off

- Check the transport bar. If it says `○ Viewport locked`, you've
  panned or paused; the viewport is no longer tracking "now". Press
  `L` or click **↩ Follow live**.

### Agents are hidden

- If you hid all agent rows via the gutter, the plot has nothing to
  draw. Open the agent gutter and un-hide. The minimap will still show
  hidden-agent rows as a reference.

### Session is live but very quiet

- The agent may be in a long thinking phase or waiting for a human
  response. Check the [current task strip](tasks-and-plans.md#currenttaskstrip)
  for status and orchestration mode. A thinking dot + no new bars is
  normal for a long reasoning step.

## Payloads are missing

**Symptom:** You open the [drawer's Payload tab](drawer.md#payload-tab)
and see either `No payload attached to this span.` or `Payload was not
preserved (client under backpressure).`

### "No payload attached"

- The span simply never carried a payload. `LLM_CALL` spans without
  prompt/response capture, `TOOL_CALL` spans without result capture,
  any custom span without an attached file — all of these have no
  refs. The tab is correct.
- If you expected a payload, confirm that the client library was
  configured to capture prompts/responses for this span kind. Some
  clients default to off.

### "Payload was not preserved (client under backpressure)"

- The client attached a payload ref but then dropped the bytes because
  of backpressure (too many in-flight uploads, memory pressure, or
  network stall).
- The ref is still there, and the payload's summary (if the client
  wrote one pre-eviction) is still shown.
- Remedy: increase the client buffer size, reduce the client's payload
  capture volume, or accept the loss — evicted payloads are permanent.

### Payload button spins forever

- The `Load full payload` button fires `getPayload` against the
  server. If the RPC never resolves, the spinner hangs.
- Check the network tab. If the request is pending, the server may be
  stuck fetching the payload from its backing store.
- If the request errored, a red error line appears under the header.

## Plan stuck / not progressing

**Symptom:** The [current task strip](tasks-and-plans.md#currenttaskstrip)
shows a RUNNING task but nothing has moved in a while. The Graph view
may show an **amber "⚠ stuck"** marker on the agent header.

### Amber border = liveness tracker flagged it

- The server's liveness tracker flags an agent as stuck when it has an
  open INVOCATION span and no recent progress signal. This is the
  honest "we think this agent is wedged" state.
- Click the agent's `↻ Status` button in the Graph view to send a
  `STATUS_QUERY` control. If the agent responds within 8 seconds,
  the task report updates and the agent is at least still alive.
- If the status query returns empty, the agent process may actually be
  hung. Check the process-side logs.

### Steering options

- A stuck agent can usually be shaken loose with a **steer** (see
  [control-actions.md](control-actions.md#steer)):
  - Popover → **Steer**, pick **Cancel & redirect**, tell the agent
    explicitly what to do next.
  - Or send a **CANCEL** control to force-terminate the invocation
    and let the planner replan.
- Be aware that `CANCEL` on a long-running production run is
  destructive — see the confirmation-policy note on
  [control-actions.md](control-actions.md#confirmation-policy).

## Drift not firing / plan revision banner not appearing

**Symptom:** You expect a plan revision (e.g. you sent a steer, or a
tool clearly errored) but the banner never shows up and the Plan
revisions section of the drawer has no new entry.

### The planner may not have recognized the drift

- Drift detection is a client-side heuristic in `HarmonografAgent`. It
  can miss signals — e.g. a tool that swallowed its own error and
  returned a success value will not produce a `tool_error` drift
  kind.
- Check the drawer's **Task tab** for the latest revision. If it says
  "last revised 10 minutes ago" and matches the kind you expected,
  you may just have missed the ~4s pill window.

### The drift kind may be unknown to the frontend

- The planner can emit drift kinds that aren't in
  `frontend/src/gantt/driftKinds.ts`. These fall back to the generic
  `Plan revised` label with a grey bullet icon. The revision **is**
  surfaced — it just doesn't have a pretty color/label yet.
- If you need better labels, add the kind to `driftKinds.ts`.

### Revisions are coming in but the banner stays empty

- The banner scans plan mutations and dedupes on `revisionReason` to
  avoid thrashing when a plan is upserted repeatedly with the same
  reason. If the reason string is identical, the pill shows once and
  then stays quiet.
- Open the drawer's Plan revisions section for the full history — the
  banner is lossy by design.

## Drawer is blank / shows "Select a span"

**Symptom:** You clicked a span but the drawer's body says
`Select a span on the Gantt to inspect it.`

### Span hasn't arrived yet

- The drawer is live-reactive: if the selected span id isn't in the
  store yet, the body renders the placeholder. As soon as the span
  streams in, the drawer re-renders automatically.
- Common when deep-linking (refresh the page with a `?span=` query
  pointing at a span that's still loading).

### Session store mismatch

- If you switch sessions while the drawer is open, the selected span
  id may not belong to the new session. Close the drawer (`Esc`) and
  reselect from the new session's Gantt.

## Attention badge is wrong

**Symptom:** The app bar's bell icon shows a count but you can't find
the attention-needing session.

- The bell aggregates `RpcSession.attentionCount` across every session
  returned by `ListSessions`. If the server's counter is stale (e.g.
  a span transitioned out of `AWAITING_HUMAN` but the session wasn't
  re-emitted), the bell can lag.
- Opening and closing the picker triggers a refresh.
- If the count is still wrong after a refresh, it's a server-side bug;
  file it against the sessions service.

## Theme / color-vision mode isn't sticking

- Theme selection uses the theme store (`frontend/src/theme/store.ts`).
  The base theme (dark / light / amoled / high-contrast) and
  color-blind mode are read from `localStorage`.
- If your browser is in private mode, the writes are dropped silently
  and the app will revert to defaults on reload. This is expected.

## Keyboard shortcuts don't work

- Shortcuts are suppressed inside input fields and textareas
  (including Material Web's `md-*-text-field` custom elements) except
  `Esc` and `⌘K`.
- The `a` and `s` shortcuts (annotate / steer) are **stubs** pending
  task #14. They won't do anything yet. See
  [keyboard-shortcuts.md](keyboard-shortcuts.md).
- The arrow-key pan shortcuts are **stubs** pending task #11. Use
  `+`/`-` or the minimap until they land.

## Session picker won't open

- Very rare — press `Esc` first in case an overlay (help, legend,
  theme menu) is already open and capturing keyboard. The `Esc` ->
  `⌘K` two-step usually clears whatever is stuck.
- If `⌘K` still does nothing, the global key handler may have been
  unmounted by a crashing component. Check the browser console.

## Where to go next

- The [index](index.md) for a tour of every region of the shell.
- [Control actions](control-actions.md) for the full list of what you can
  push back to an agent.
- The code itself. `frontend/src/components/` is ground truth; every
  claim in this guide is derived from it.
