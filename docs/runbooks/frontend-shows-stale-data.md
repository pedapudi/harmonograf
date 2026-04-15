# Runbook: Frontend shows stale data

The server has fresh data (visible in sqlite, in server logs, in
`make stats`) but the UI is showing an older state. Refreshing the
page fixes it temporarily.

## Symptoms

- **UI**: a plan, task, span, or agent status in the UI disagrees
  with what `sqlite3 data/harmonograf.db` says.
- **Browser DevTools**: `WatchSession` stream is open and showing
  events, but the component you're looking at doesn't re-render.
- **Server**: logs show the delta was published (see `bus.py` calls to
  `publish_*`), `make stats` confirms.

## Immediate checks

```bash
# Is the server actually publishing?
grep -E 'publish_|task plan received|plan upserted|delta' data/harmonograf-server.log | tail -30

# Is WatchSession streaming?
# (DevTools Network tab, filter for harmonograf.v1.Harmonograf/WatchSession)

# Force a UI reload and compare:
sqlite3 data/harmonograf.db \
  "SELECT id, revision_index FROM task_plans WHERE session_id='SESSION_ID' ORDER BY revision_index DESC LIMIT 3;"
```

## Root cause candidates (ranked)

1. **WatchSession stream detached** — the frontend subscribed once,
   then the stream dropped and auto-reconnect didn't re-fire every
   delta. Common after a browser tab has been idle long enough for the
   server to time out its subscription.
2. **TaskRegistry subscribers not firing** — `computePlanDiff` and
   `TaskRegistry.upsert` produce a diff and notify; if the drawer /
   minimap forgot to subscribe, it stays stale. See
   `frontend/src/gantt/index.ts :: computePlanDiff`.
3. **SessionStore staleness** — the store tracks `minTimeMs` /
   `maxTimeMs`; if the computed window doesn't include the new data,
   the renderer filters it out. Check for `Infinity` in the time
   bounds (see `dev-guide/debugging.md` §"The Gantt is empty").
4. **Session ID changed but UI still points at the old one** — the
   user selected a session from the picker; a newer session was
   created; the picker shows the new name but the URL / uiStore
   still references the old ID. Some components read from the URL,
   others from the store.
5. **Hidden behind a filter** — the user has hidden the agent's row
   or is looking at a frozen viewport.
6. **Attention count stale** — the bell icon counts
   `RpcSession.attentionCount` across sessions; if one session's
   counter isn't re-emitted, the bell lags
   (`user-guide/troubleshooting.md` §"Attention badge is wrong").
7. **Frontend built against an older proto** — a frontend JS bundle
   compiled against an old proto descriptor silently ignores new
   fields. The server emits them but the UI ignores them.

## Diagnostic steps

### 1. Stream detached

DevTools → Network → find `WatchSession`. Status should be "pending"
(i.e. open stream). If it's "complete" or errored, the stream is
dead. Look at the response headers for a gRPC status code.

### 2. TaskRegistry subscription

Open DevTools console and run:

```js
// If you expose the registry on window for debugging:
window.__taskRegistry?.subscribers?.size
```

If not exposed, add a `console.log` to `computePlanDiff` and reload.
If the log doesn't fire after a known server event, the diff isn't
being computed.

### 3. SessionStore bounds

DevTools console:

```js
localStorage.getItem('harmonograf.session')
// or whatever key uiStore uses.
```

Also inspect `sessionStore.minTimeMs` / `maxTimeMs`. If they're
`Infinity` / `-Infinity`, no data has "landed" per the store; press
`f` to fit.

### 4. Wrong session ID

URL address bar → look for `?session=`. Compare to the picker's
selected session. If they disagree, force-reload.

### 5. Filter / hidden

Check the agent gutter; ensure no agent is hidden. Press `L` to
re-enable live follow if the transport bar shows `○ Viewport locked`.

### 6. Attention count

Open/close the session picker; this forces a `ListSessions` call
which should refresh counts.

### 7. Old proto

```bash
grep '"@bufbuild/protobuf"\|proto' frontend/package.json
# Then rebuild:
cd frontend && pnpm run build
```

## Fixes

1. **Stream detached**: implement or verify the reconnect path in
   `rpc/hooks.ts`. As a user workaround, reload the page.
2. **Subscribers**: wire the component to `TaskRegistry.subscribe`
   and ensure it re-renders on notify.
3. **SessionStore bounds**: fit-to-data (`f`) and pan/zoom to expose
   new spans.
4. **Wrong session ID**: update the URL or pick the new session
   explicitly.
5. **Filter**: un-hide; enable live follow.
6. **Attention count**: refresh the picker; if still wrong, it's a
   server-side bug in `rpc/frontend.py` session emission.
7. **Old proto**: rebuild the frontend and redeploy.

## Prevention

- Add an integration test: server publishes a plan delta, frontend
  re-renders within 500ms.
- Expose `TaskRegistry` / `SessionStore` on `window` in dev builds so
  operators can poke at state from DevTools.
- Run the frontend against the server with a scheduled restart to
  exercise the stream reconnect path; don't let it rot.

## Cross-links

- [`dev-guide/debugging.md`](../dev-guide/debugging.md) §"The UI shows
  stale data after a refine".
- [`user-guide/troubleshooting.md`](../user-guide/troubleshooting.md)
  §"Attention badge is wrong".
- [`dev-guide/frontend.md`](../dev-guide/frontend.md) for the store /
  registry shape.
