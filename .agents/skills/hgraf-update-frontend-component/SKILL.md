---
name: hgraf-update-frontend-component
description: Pattern for adding a new shell component — wire into TaskRegistry or sessionsStore, place in Shell/AppBar/Drawer, add vitest, keep tsc + eslint clean.
---

# hgraf-update-frontend-component

## When to use

You are adding or modifying a React component under `frontend/src/components/shell/` — for example, a new Drawer tab, an overlay toggle in the AppBar, a status pill in the nav rail, or a new visualization view under `shell/views/`.

## Prerequisites

1. `pnpm install` has run (`make frontend-install` or `cd frontend && pnpm install --frozen-lockfile`).
2. Familiarize yourself with the existing shell components (inventory):
   - `Shell.tsx` — top-level layout (app bar + nav rail + drawer + main view)
   - `AppBar.tsx` — top bar (session selector, buttons)
   - `NavRail.tsx` — left-side section switcher (sessions/activity/graph/annotations/settings)
   - `Drawer.tsx` — right-side details panel; mounts `OrchestrationTimeline` among others
   - `CurrentTaskStrip.tsx` — shows active task from `TaskRegistry` + state_protocol keys
   - `PlanRevisionBanner.tsx` — drift banner, reads `TaskRegistry` revisions
   - `ErrorBoundary.tsx` — crash guard
   - `HelpOverlay.tsx` — keyboard shortcuts help
   - `OrchestrationTimeline.tsx` — reporting-tool event timeline in the drawer
   - `views/GanttView.tsx`, `views/ActivityView.tsx`, `views/GraphView.tsx` — main view bodies
3. Understand the state plumbing. Components subscribe to one or more of:
   - `useSessionsStore` (`frontend/src/state/sessionsStore.ts`) — session list, current session id
   - `useUiStore` (`frontend/src/state/uiStore.ts:91-150+`) — nav section, drawer open/closed, overlay toggles, persisted via localStorage
   - `TaskRegistry` + `AgentRegistry` (`frontend/src/gantt/index.ts:24-250+`) — session-scoped plan + agent state, subscribe via `.subscribe(callback)` returning an unsubscribe
   - `useAnnotationStore`, `usePopoverStore` — smaller scoped stores

## Step-by-step

### 1. Pick the parent and the data source

- **AppBar** — for global toggles (e.g. theme, view options, session switcher).
- **NavRail** — for new top-level sections. You will also extend `NavSection` at `uiStore.ts:7-14`.
- **Drawer** — for details panels about the selected span / task / agent.
- **Main view** (`views/`) — for a brand new visualization.
- **A new standalone** — only if it doesn't fit any of the above.

The data source determines subscription:
- Session list → `useSessionsStore`
- UI toggles → `useUiStore` (add a new field + persisted-localStorage helpers following `readGraphViewport` / `writeGraphViewport` at `uiStore.ts:18-89`)
- Task plan → `TaskRegistry` per-session (construct via `new TaskRegistry()` somewhere high in the tree, or use the existing instance — grep for `new TaskRegistry` to find it)
- Agent state → `AgentRegistry` (same pattern; `gantt/index.ts:24-103`)

### 2. Create the component file

```tsx
// frontend/src/components/shell/MyComponent.tsx
import { useEffect, useState } from "react";
import { useUiStore } from "../../state/uiStore";
import type { TaskRegistry } from "../../gantt";

interface Props {
  taskRegistry: TaskRegistry;
  sessionId: string;
}

export function MyComponent({ taskRegistry, sessionId }: Props) {
  const [plan, setPlan] = useState(() => taskRegistry.getPlan(sessionId));

  useEffect(() => {
    const unsubscribe = taskRegistry.subscribe(sessionId, (next) => setPlan(next));
    return unsubscribe;
  }, [taskRegistry, sessionId]);

  if (!plan) return null;
  return <div>{plan.tasks.length} tasks</div>;
}
```

**Subscription pattern:** always return the unsubscribe from `useEffect`. `TaskRegistry.subscribe()` is designed to hand back an unsubscribe function (grep for the implementation around `gantt/index.ts:183-250` to confirm the signature).

**Rendering pattern:** prefer controlled rendering over imperative DOM. The one exception is the Gantt canvas itself, which is imperative by necessity (see `GanttCanvas.tsx`).

### 3. Mount in the parent

Edit `Shell.tsx` (or `AppBar.tsx` / `Drawer.tsx` / `NavRail.tsx`) and import + render your component. Pass the `TaskRegistry` instance from wherever it lives in the component tree — do not construct a new one per mount (the registry holds the canonical state).

If you are adding to the drawer as a new tab, look at the current tab switcher logic in `Drawer.tsx` and add your tab label + body.

### 4. Wire persisted state (if needed)

If your component owns a toggle that should persist across reloads, add a field to `UiState` in `uiStore.ts`. Follow the `graphViewport`/`taskPlanMode`/`taskPlanVisible` patterns at lines 18-89:

```ts
const MY_TOGGLE_KEY = "harmonograf.ui.myComponent.expanded";
const readMyToggle = () => localStorage.getItem(MY_TOGGLE_KEY) === "1";
const writeMyToggle = (v: boolean) => localStorage.setItem(MY_TOGGLE_KEY, v ? "1" : "0");
```

Add to Zustand state with an initializer calling `readMyToggle()` and a setter calling `writeMyToggle()`.

### 5. Tests

Add a vitest under `frontend/src/__tests__/` (check existing layout). Structure:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { MyComponent } from "../components/shell/MyComponent";
import { TaskRegistry } from "../gantt";

describe("MyComponent", () => {
  it("renders task count", () => {
    const tr = new TaskRegistry();
    tr.upsertPlan("s1", { tasks: [...], edges: [], ... });
    render(<MyComponent taskRegistry={tr} sessionId="s1" />);
    expect(screen.getByText(/1 tasks/)).toBeInTheDocument();
  });
});
```

Keep tests hermetic — construct a fresh `TaskRegistry` per test. Do not mock the whole module.

### 6. Keep tsc + eslint clean

```bash
cd frontend
pnpm lint             # eslint
pnpm build            # tsc + vite build (full typecheck)
```

Both must pass for CI. The `frontend-test` Make target (`cd frontend && pnpm build && pnpm lint`) is what CI runs.

### 7. Live smoke

```bash
make demo
```

Open `http://127.0.0.1:5173`, drive a scenario, confirm:
- The component renders where you expected.
- Subscription updates: when the server pushes a new task, your component re-renders.
- Persisted toggles survive a hard reload.
- No console errors.

## Verification

```bash
cd frontend
pnpm lint
pnpm build
pnpm test  # if vitest is wired; check package.json scripts

cd ..
make demo
# manual smoke in browser
```

## Common pitfalls

- **Forgetting the unsubscribe.** `TaskRegistry.subscribe` returns an unsubscribe. If you don't return it from `useEffect`, you leak subscriptions every time the component remounts, and stale component instances keep updating (React will warn about "Can't perform a state update on an unmounted component").
- **Constructing a fresh `TaskRegistry` per mount.** The registry holds canonical state. A per-mount instance starts empty. Always thread the singleton down from the top-level component (usually `App.tsx` or `Shell.tsx`).
- **Reading Zustand stores outside a React render.** `useUiStore()` is a hook — cannot be called in an event handler. Use `useUiStore.getState()` for imperative reads.
- **localStorage race on first boot.** If your initializer reads from localStorage *before* the value is set, the default wins. SSR-style hydration is not relevant here (Vite dev is client-only) but hot-reload can cause a stale read. Always defensively default.
- **Drawer tab conflicts.** The drawer tab list is defined in one place. Adding a tab means editing that list; forgetting to add the body means the tab switcher silently shows nothing.
- **Directly editing `frontend/src/pb/*.ts`.** These are generated — your changes are wiped by `make proto`. Edit domain types at `gantt/types.ts` and rpc converters at `rpc/convert.ts` instead.
- **`tsc` passes, `vite build` fails.** Vite bundler config is slightly stricter. If `pnpm lint` and `pnpm build` disagree, trust `pnpm build` — it is what CI runs.
- **CSS bleed.** Global styles live in `index.css`. Component-specific styles should be colocated (CSS modules or inline styles). Adding global selectors for your component is a common regression source.
- **Snapshot tests that capture timestamps.** If your component renders `started_at` in a human format, snapshot tests will flake across timezones / CI runs. Format to a fixed offset in tests, or assert on parts.
