# Runbook: Minimap desync

The sequence-view minimap (`frontend/src/components/Gantt/Minimap.tsx`)
shows a viewport rectangle that doesn't match the main Gantt's
viewport. Panning the main view doesn't update the minimap, or
dragging on the minimap doesn't move the main view.

**Triage decision tree** — minimap state lives in two surfaces; desync is
almost always one source-of-truth bug.

```mermaid
flowchart TD
    Start([Minimap viewport<br/>≠ main Gantt viewport]):::sym --> Q1{localStorage.clear()<br/>fixes it?}
    Q1 -- "yes" --> F1[Persisted viewport bled<br/>across sessions: key by<br/>session_id]:::fix
    Q1 -- "no" --> Q2{Main and minimap viewport<br/>numbers off by ~1000×?}
    Q2 -- "yes" --> F2[Time-unit mismatch:<br/>milliseconds everywhere]:::fix
    Q2 -- "no" --> Q3{Resize browser:<br/>minimap redraws?}
    Q3 -- "no" --> F3[Missing ResizeObserver<br/>on container]:::fix
    Q3 -- "yes" --> Q4{Hide / unhide agent rows:<br/>vertical scale updates?}
    Q4 -- "no" --> F4[Cached row-count;<br/>read live]:::fix
    Q4 -- "yes" --> Q5{Press 'f': both surfaces<br/>jump in same frame?}
    Q5 -- "no" --> F5[Subscription gap:<br/>minimap reads derived copy.<br/>Subscribe to main store]:::fix
    Q5 -- "yes" --> F6[Two stores own viewport:<br/>unify, delete duplicate]:::fix

    classDef sym fill:#fde2e4,stroke:#c0392b,color:#000
    classDef fix fill:#d4edda,stroke:#27ae60,color:#000
```

## Symptoms

- **UI**: the minimap's highlighted rectangle is offset, wrong
  width, or stuck; clicking on the minimap jumps to the wrong
  time; the minimap shows old spans that were already scrolled past.
- **DevTools console**: no errors necessarily — this is often a
  silent state-sync bug.

## Immediate checks

```js
// Browser DevTools console:
// Inspect uiStore (exact name depends on implementation)
Object.keys(localStorage).filter(k => k.includes('harmonograf'))

// Look at current viewport state
JSON.stringify(window.__sessionStore?.viewport)
```

Also in DevTools:
- Elements tab → inspect the Minimap component; check the
  `transform` / `x` / `width` props on the viewport rectangle.
- Components tab (React DevTools) → look at Minimap and GanttCanvas
  state side-by-side.

## Root cause candidates (ranked)

1. **Two sources of truth** — the main Gantt reads its viewport
   from `SessionStore` but the Minimap reads from `uiStore` (or
   vice versa). When they diverge, you see desync. Grep for
   `viewport` in `frontend/src/state/`.
2. **Stale transform after viewport reset** — hitting "fit to
   data" (`f`) or a session switch resets the main view but the
   minimap's cached bounds lag one frame.
3. **Time unit mismatch** — one surface uses milliseconds, the
   other uses seconds. The numeric ratio looks almost-right but is
   off by 1000.
4. **Hidden-agent rows changing minimap height** — toggling agent
   visibility changes the main Gantt's row count but the minimap
   was computing its vertical scale from a cached row count.
5. **localStorage-persisted viewport from a different session** —
   uiStore persisted the viewport from session A; you opened
   session B; the Minimap read the old persisted value before the
   store refreshed.
6. **Resize handler not attached** — the minimap sizes itself from
   a parent container; if the container resized without firing a
   resize observer, the minimap's canvas has the wrong width.
7. **Live-follow interaction** — live-follow mode pins the main
   viewport to "now"; if the minimap doesn't know the pin flag,
   its rectangle chases the old non-following state.

## Diagnostic steps

### 1. Two sources

```bash
grep -rn 'viewport' frontend/src/state/ frontend/src/components/Gantt/
```

Identify the store(s) that own viewport state. There should be
one.

### 2. Stale transform after reset

Press `f` with DevTools open. Watch both the main canvas and the
Minimap. If the main jumps immediately but the Minimap catches up
only after another action, you have a subscription gap.

### 3. Time units

Log both viewports' left/right bounds:

```js
console.log('main:', window.__gantt?.viewport, 'mini:', window.__minimap?.viewport)
```

If one is ~1000× the other, you found the unit bug.

### 4. Hidden rows

Hide/unhide agent rows and watch the minimap vertical scale. If it
doesn't update, the row-count source is cached.

### 5. Persisted state

```js
localStorage.clear()
// then reload
```

If the desync goes away, a stale persisted value was the cause.
Narrow down which key.

### 6. Resize observer

Resize the browser window while watching the minimap. If it
doesn't redraw, the observer isn't firing.

### 7. Live-follow pin

Toggle live-follow (`L`) and see whether the minimap's rectangle
updates to match.

## Fixes

1. **Two sources**: unify on one store. Delete the duplicate.
2. **Stale transform**: make the minimap subscribe to the main
   viewport store directly, not a derived copy.
3. **Units**: pick milliseconds everywhere. Search for unit
   divisions in the minimap code and normalise.
4. **Hidden rows**: make the minimap read row count live, not from
   a ref.
5. **Persisted state**: key the persisted viewport by session_id
   so it doesn't bleed across sessions.
6. **Resize observer**: wrap the minimap container in a
   `ResizeObserver`.
7. **Live-follow**: propagate the pin flag into the minimap props.

## Prevention

- One store for viewport. Cross-surface writes go through it.
- Snapshot-testing the minimap at known viewport states in CI.
- Key any localStorage that holds session-scoped state with the
  session id.

## Cross-links

- Task #1 milestone — "sequence diagram minimap + zoom" (completed).
- [`user-guide/gantt-view.md`](../user-guide/gantt-view.md) if it
  documents the minimap, or
  [`dev-guide/frontend.md`](../dev-guide/frontend.md).
- `frontend/src/components/Gantt/Minimap.tsx`.
