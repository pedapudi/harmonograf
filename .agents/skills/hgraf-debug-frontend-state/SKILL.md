---
name: hgraf-debug-frontend-state
description: Introspect harmonograf frontend state — SessionStore, TaskRegistry, uiStore, GanttRenderer subscriptions — when the UI and the server data disagree.
---

# hgraf-debug-frontend-state

## When to use

The server shows one thing in sqlite or on `WatchSession`, but the frontend shows something else: missing spans, stale task status, wrong agent order, drawer open on the wrong tab, an invariant icon that won't clear. You need to look inside the live frontend state to find the divergence.

## Prerequisites

1. Dev server running: `pnpm -C frontend dev` (usually `http://localhost:5173`). Backend running locally: `uv run harmonograf-server`.
2. Chrome/Firefox DevTools open on the tab.
3. Read `frontend/src/gantt/index.ts:1-120` to understand the `SessionStore` shape: `agents`, `spans`, `tasks`, `plans`, `annotations`, each a subscribe-able collection.
4. Read `frontend/src/state/uiStore.ts` for the zustand store that owns UI-only state (drawer open, selected span, viewport, focused agent).
5. Read `frontend/src/rpc/hooks.ts` — `getSessionStore`, `useSessionWatch`, `useHarmonografClient` are the top-level access points.

## Step-by-step

### 1. Pull the SessionStore out in the console

The renderer keeps per-session stores in a module-level map. Access it in DevTools console:

```js
// From src/rpc/hooks.ts
import('/src/rpc/hooks.ts').then(m => window._store = m.getSessionStore(window._sessionId || 'sess_…'));
```

Easier: grab the currently-focused session id off the uiStore first:

```js
const ui = (await import('/src/state/uiStore.ts')).useUiStore.getState();
window._sid = ui.currentSessionId;
const { getSessionStore } = await import('/src/rpc/hooks.ts');
window._store = getSessionStore(window._sid);
console.log('agents:', window._store.agents.list);
console.log('spans:', window._store.spans.countAll?.() ?? '(no countAll)');
console.log('tasks:', window._store.tasks);
```

### 2. Dump spans for one agent

`SessionStore.spans.queryAgent(agentId, startMs, endMs)` returns the spans the renderer is actually drawing:

```js
const [agent] = window._store.agents.list;
const spans = window._store.spans.queryAgent(agent.id, -Infinity, Infinity);
console.table(spans.map(s => ({ id: s.id, kind: s.kind, status: s.status, start: s.startMs, end: s.endMs })));
```

Compare against `curl` output from `GetSession` (`server/harmonograf_server/rpc/frontend.py`) for the same session — if the server has spans the frontend doesn't, the gap is in `WatchSession` delta handling (`rpc/hooks.ts :: useSessionWatch`) or in `rpc/convert.ts` dropping unknown fields.

### 3. Watch live updates with subscribe()

Every collection in `SessionStore` is a lightweight subject:

```js
const unsub = window._store.spans.subscribe(() => {
  console.log('spans updated, now', window._store.spans.queryAgent(agent.id, -Infinity, Infinity).length);
});
// Later:
unsub();
```

If the `subscribe` callback never fires but the server is streaming, the `WatchSession` call isn't delivering deltas. Check the Network tab for the `WatchSession` fetch and its response body.

### 4. TaskRegistry / plan diff

Tasks and plans live on `SessionStore.tasks` and `SessionStore.plans`. The live task state comes from `TaskRegistry` (grep `class TaskRegistry` in `frontend/src/gantt/index.ts`). Dump:

```js
console.log('current plan:', window._store.getCurrentPlan?.());
console.log('tasks:', window._store.tasks.list);
console.log('plan diff:', window._store.plans.currentDiff?.());
```

A plan revision diff lives at `frontend/src/gantt/index.ts :: computePlanDiff` — if the Refine banner shows wrong deltas, that's the place to check.

### 5. uiStore inspection

```js
const { useUiStore } = await import('/src/state/uiStore.ts');
console.log(useUiStore.getState());
```

Useful fields to check:

- `drawerOpen`, `selectedSpanId`, `drawerRequestedTab`, `drawerRequestedTaskSubtab` — drawer deep-linking state.
- `currentSessionId`, `focusedAgentId`, `sessionPickerOpen`.
- `navSection` — `'sessions' | 'activity' | 'graph' | 'annotations' | 'settings'`.
- `graphViewport` — persisted to `localStorage` under `harmonograf.graphViewport`.
- `helpOpen`, `liveFollow`.

Mutate from the console to test a hypothesis:

```js
useUiStore.setState({ drawerOpen: true, selectedSpanId: 'span-123' });
```

### 6. GanttRenderer

The renderer itself (`frontend/src/gantt/renderer.ts`, 1828 lines) is a class stored on the uiStore (`graphActions` / `ganttRenderer` handles). Check:

```js
const r = useUiStore.getState().ganttRenderer;
console.log('viewport:', r?.getViewport?.());
console.log('layout:', r?.layoutSnapshot?.());
```

The renderer has `subscribe(listener)` for external consumers. If a component isn't re-rendering on span arrival, verify the listener is attached (React DevTools → inspect component → Hooks panel).

### 7. React DevTools for component-level state

Install the React DevTools browser extension. Key things to inspect:

- `<Drawer>` — `tab` state (`useState<TabId>`) should match `drawerRequestedTab` at mount.
- `<GraphView>` — the `store` prop identity changes when the session switches; if it doesn't, `useSessionWatch` is over-memoizing.
- `<TransportBar>` — `liveFollow` boolean.

### 8. Session watch refcount

`useSessionWatch` refcounts watchers — when two components watch the same session the underlying stream is shared. If one component forgets to unsubscribe, the stream stays open. Log the refcount:

```js
(await import('/src/rpc/hooks.ts')).debugWatchRefcount?.(window._sid);
```

(If `debugWatchRefcount` doesn't exist, grep `rpc/hooks.ts` for a `refcount` or `_watchers` variable and read it directly.)

### 9. Server-side sanity check

When in doubt, hit the server directly:

```bash
curl -s http://localhost:8080/harmonograf.v1.FrontendService/GetSession \
  -H "content-type: application/json" \
  --data '{"sessionId":"sess_abc"}' | jq .
```

If the server response matches what you see in the frontend store, the bug is client-side. If not, the bug is server-side; jump to the ingest pipeline (`server/harmonograf_server/ingest.py`) and the store layer.

## Common pitfalls

- **Stale module cache**: HMR occasionally keeps an old `SessionStore` alive. A full reload (`Ctrl+Shift+R`) resolves it; if not, kill `pnpm dev` and restart.
- **Assuming zustand selectors re-render**: `useUiStore((s) => s.drawerOpen)` only triggers on that field. If you select an object `{...}`, the identity changes every tick and you get infinite loops.
- **Subscribing without unsub**: `SessionStore.spans.subscribe(() => ...)` returns an unsubscribe function. Forgetting to call it leaks across navigation.
- **LocalStorage poisoning**: the graph viewport persists to `localStorage.harmonograf.graphViewport`. Bad values there will make the graph view open off-screen. `localStorage.clear()` is the nuclear fix; per-key `removeItem` is surgical.
- **`getSessionStore` returns undefined**: the store is lazy-created on first `useSessionWatch`. Open the session in the UI before inspecting in the console.
- **Reading the renderer during a frame**: `GanttRenderer` mutates layout arrays in place during paint. Reading them inside a `requestAnimationFrame` callback is racy. Read from outside rAF or snapshot first.
