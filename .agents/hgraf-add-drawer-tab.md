---
name: hgraf-add-drawer-tab
description: Add a new tab to the span inspector drawer — TabId, TABS registry, body component, uiStore deep-link, tests.
---

# hgraf-add-drawer-tab

## When to use

You need a new panel inside the span inspector drawer (the right-hand slide-out). Existing tabs: Summary, Task, Payload, Timeline, Links, Annotations, Control. This is a narrow recipe — see `hgraf-update-frontend-component.md` in the same directory for the general frontend-component pattern this specializes.

## Prerequisites

1. Read `frontend/src/components/shell/Drawer.tsx:29-46` — `TabId` union, `TABS` constant.
2. Read `frontend/src/components/shell/Drawer.tsx:141-187` — `DrawerTabs` rendering and the switch over `tab`.
3. Read `frontend/src/state/uiStore.ts` to find the `drawerRequestedTab` / `openDrawerOnTrajectory` pattern — that's how keyboard shortcuts deep-link to a specific tab.

## Step-by-step

### 1. Extend `TabId`

`frontend/src/components/shell/Drawer.tsx:29`:

```tsx
type TabId =
  | 'summary'
  | 'task'
  | 'payload'
  | 'timeline'
  | 'links'
  | 'annotations'
  | 'control'
  | 'thinking'; // new
```

### 2. Register in `TABS`

`Drawer.tsx:38`:

```tsx
const TABS: { id: TabId; label: string; testId: string }[] = [
  // ...existing entries...
  { id: 'thinking', label: 'Thinking', testId: 'inspector-tab-thinking' },
];
```

The `testId` must be a unique string; playwright/vitest tests reference it via `getByTestId`.

### 3. Write the tab body component

Below the other `XxxTab` components in the same file:

```tsx
function ThinkingTab({ span, sessionId }: { span: Span; sessionId: string | null }) {
  // Use useSessionWatch if you need live updates on the span itself.
  // Use useUiStore selectors for UI-only state.
  return (
    <div className="hg-drawer__body" data-testid="inspector-thinking-body">
      {/* ... */}
    </div>
  );
}
```

For tabs that need live updates on the selected span (status/attribute mutations), use `useSessionWatch(sessionId)` and re-read the span via `store.spans.getById(span.id)` inside an effect — see how `CurrentTaskSection` does it at `Drawer.tsx:189`.

For tabs that subscribe to collections, use `useReducer((x: number) => x + 1, 0)` + `store.tasks.subscribe(() => bump())` (the same `[, bump] = useReducer` pattern at `Drawer.tsx:200`).

### 4. Wire it into `DrawerTabs`

`Drawer.tsx:173`:

```tsx
{tab === 'thinking' && <ThinkingTab span={span} sessionId={sessionId} />}
```

### 5. Optional: deep-link from a shortcut

If you want a keyboard shortcut to open the drawer directly on your tab, extend `uiStore.ts` with a new action like `openDrawerOnThinking(spanId)` mirroring the existing `openDrawerOnTrajectory` (grep in `uiStore.ts`):

```ts
openDrawerOnThinking(spanId: string) {
  set({ drawerOpen: true, selectedSpanId: spanId, drawerRequestedTab: 'thinking' });
},
```

Then in `frontend/src/lib/shortcuts.ts` add a binding following the `thinking-trajectory` precedent at `shortcuts.ts:272`:

```ts
{
  id: 'thinking-tab',
  description: 'Open drawer on Thinking tab',
  combo: 'shift+t',
  handler: () => {
    const s = ui();
    const spanId = s.selectedSpanId ?? listAllSpans(s.currentSessionId)[0]?.id;
    if (spanId) s.openDrawerOnThinking(spanId);
  },
},
```

Update `HelpOverlay` in `frontend/src/components/shell/HelpOverlay.tsx` with the new shortcut if it exists in the help table (it likely lists `defaultShortcuts()` directly — a test will catch you if it doesn't).

### 6. Consume `drawerRequestedTab`

The drawer already reads `drawerRequestedTab` on mount (`Drawer.tsx:145`) and calls `consumeDrawerRequestedTab()` on the next microtask. Your new `TabId` just works because the consumer stores whatever string the uiStore handed it.

If your tab has sub-state (like `TaskTab` has `drawerRequestedTaskSubtab`), add a parallel field on `uiStore` and a second `consume` call. Follow `TaskTab`'s precedent.

### 7. Tests

- `frontend/src/__tests__/components/Drawer.test.tsx` — drawer renders the new tab button, clicking it swaps the body, and `openDrawerOnThinking` puts the drawer on the right tab at mount.
- If you added a shortcut, extend `frontend/src/__tests__/lib/shortcuts.test.ts` to fire the keybinding and assert uiStore state.

### 8. Verification

```bash
cd frontend && pnpm test -- --run Drawer
cd frontend && pnpm typecheck
cd frontend && pnpm dev  # manually verify the tab renders and the shortcut opens it
```

## Common pitfalls

- **Tab order matters visually**: `TABS` is rendered in array order. If your new tab belongs in the middle (e.g., next to "Task"), insert at the right index rather than always appending.
- **Forgetting the default tab**: `useState<TabId>(requestedTab ?? 'summary')` (`Drawer.tsx:147`) falls back to `'summary'`. If you want your new tab to be the default for certain selections, gate that in the `requestedTab ??` expression.
- **Session-less tabs**: `sessionId` can be `null` when the drawer opens via a deep link before the session has loaded. Guard every `getSessionStore(sessionId)` call. See how `AnnotationsTab` does `sessionId &&` at `Drawer.tsx:178`.
- **Reacting to live span changes**: the bare `span` prop is snapshotted at the drawer's last render. For tabs that need live data, route through `useSessionWatch` and look up `store.spans.getById(span.id)` inside the tab body.
- **Adding to `TABS` but not the switch**: the compiler won't catch you because `TabId` is exhaustive in the switch only if you write `never` fallthrough. Add a test that clicks every tab in `TABS` and asserts its body test-id is visible.
