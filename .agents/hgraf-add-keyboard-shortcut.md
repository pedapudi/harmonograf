---
name: hgraf-add-keyboard-shortcut
description: Add or change a global keyboard shortcut — shortcuts.ts registry, uiStore action, HelpOverlay entry, tests.
---

# hgraf-add-keyboard-shortcut

## When to use

You're adding a new keybinding to the harmonograf console. All global shortcuts live in a single table (`frontend/src/lib/shortcuts.ts`) and are installed once via `useGlobalShortcuts()` at app mount. No component-local key handlers — everything goes through this one table so HelpOverlay can render the complete list.

## Prerequisites

1. Read `frontend/src/lib/shortcuts.ts:1-35` — the `ShortcutBinding` interface, `comboMatches`, the `mod+` (Cmd on macOS / Ctrl elsewhere) convention.
2. Read `shortcuts.ts:303-332 useGlobalShortcuts` — the single global keydown listener that iterates bindings and runs the first matching handler.
3. Read `shortcuts.ts:77-301 defaultShortcuts` — the full binding table; every key in the product is here.
4. Know where the handler's target state lives. Shortcuts typically drive `useUiStore` actions, renderer actions on `ganttRenderer`, or the session picker.

## Step-by-step

### 1. Pick a combo

Conventions used in the existing table:

- Single letter for verbs: `a` annotate, `s` steer, `f` fit, `l` live, `t` thinking trajectory.
- `j` / `k` for sequential navigation (span next/prev).
- `[` / `]` for agent row navigation.
- `g` / `shift+g` for first/last.
- `mod+k` for palette-style actions (session picker).
- `mod+=` / `mod++` / `mod+-` / `mod+0` for zoom.
- Arrow keys for viewport pan.
- Space for live-follow toggle.
- `shift+?` for help.

Avoid single modifiers like `shift` on their own. Avoid `alt` unless the binding is specifically for advanced users — alt conflicts with macOS special-character entry.

Check for collisions:

```bash
grep -n "combo: '" frontend/src/lib/shortcuts.ts
```

### 2. Decide where the handler's state lives

If the handler mutates UI state, add a method on `useUiStore` first (`frontend/src/state/uiStore.ts`). Do not call `useUiStore.setState({...})` directly from inside a shortcut handler unless the change is a simple field flip — that breaks the invariant that uiStore actions are the public API.

Example uiStore action:

```ts
toggleMinimap(): void {
  set((s) => ({ minimapVisible: !s.minimapVisible }));
},
```

### 3. Add the binding

`shortcuts.ts:79` inside `defaultShortcuts()`:

```ts
{
  id: 'toggle-minimap',
  description: 'Toggle Gantt minimap',
  combo: 'm',
  handler: () => ui().toggleMinimap(),
},
```

- `id`: unique stable string, used by HelpOverlay and tests.
- `description`: one line for the help screen — be action-oriented ("Pan 10% left", not "Arrow left").
- `combo`: lowercase, `key` names from `KeyboardEvent.key` — `arrowleft`, `escape`, ` ` (literal space), `shift+?`.
- `handler`: fires on exact match. `comboMatches` checks modifier exclusivity — `mod+k` does NOT match plain `k`.

### 4. Deep-linking a view or drawer

If the shortcut should open the drawer on a specific tab (like the `t` → Trajectory binding), add an `openDrawerOnX(spanId)` action on `uiStore` mirroring `openDrawerOnTrajectory`, then call it from the handler:

```ts
{
  id: 'thinking-tab',
  description: 'Open drawer on Thinking tab',
  combo: 'shift+t',
  handler: () => {
    const s = ui();
    if (!s.currentSessionId) return;
    const spanId = s.selectedSpanId ?? listAllSpans(s.currentSessionId)[0]?.id ?? null;
    if (spanId) s.openDrawerOnThinking(spanId);
  },
},
```

The existing `thinking-trajectory` binding at `shortcuts.ts:272` is the template.

### 5. Navigation shortcuts

For j/k-style traversal, use the existing `neighborSpan` / `neighborAgent` helpers at `shortcuts.ts:54`. They traverse `getSessionStore(sessionId)` in the same stable order the renderer uses. Don't re-implement traversal — the helpers keep ordering consistent across views.

### 6. Editable-field guard

The global listener (`shortcuts.ts:307-320`) suppresses most shortcuts while an input / textarea / contenteditable is focused. Escape and `mod+k` are explicit exceptions. If your new shortcut should *also* work while typing (rare — prefer not), add it to the exception list:

```ts
if (e.key !== 'Escape' && !(e.key.toLowerCase() === 'k' && (e.metaKey || e.ctrlKey)) && !myException) {
  return;
}
```

Most new shortcuts should fall through normally.

### 7. HelpOverlay

`frontend/src/components/shell/HelpOverlay.tsx` reads `defaultShortcuts()` and renders the table. The new binding appears automatically. Verify by checking the HelpOverlay test for a snapshot of the rendered list.

### 8. Tests

`frontend/src/__tests__/lib/shortcuts.test.ts`:

```ts
it('m toggles minimap', () => {
  const e = new KeyboardEvent('keydown', { key: 'm' });
  document.dispatchEvent(e);
  expect(useUiStore.getState().minimapVisible).toBe(true);
});
```

Use the existing test setup — install the global listener in a `beforeEach`, clean up in `afterEach`.

If HelpOverlay has a snapshot, update it (`--update-snapshot`).

### 9. Verification

```bash
cd frontend && pnpm test -- --run shortcuts HelpOverlay
cd frontend && pnpm typecheck
cd frontend && pnpm dev  # manually press the combo on every view
```

## Common pitfalls

- **Mod conflict**: `mod+k` on macOS is Cmd-K; on Linux it's Ctrl-K. `comboMatches` handles this, but don't use `mod+w` (Cmd-W closes the tab) or `mod+s` (Save page). Test on the actual OS you care about.
- **Combo ordering**: the first matching binding wins. If you add `mod+k` and also `k`, the plain `k` binding runs whenever mod is not held. If the order matters, put the more specific binding first.
- **Special keys**: `' '` (literal space) is the combo for space. `arrowleft`, `arrowright`, `escape`, `enter` are the names. `?` combined with shift should be `shift+?`, not `shift+/` (even though that's the physical key). `KeyboardEvent.key` gives you the logical key.
- **Handler referencing stale state**: `defaultShortcuts()` runs once inside `useEffect([])`. If the handler closes over a variable, that variable is the initial value forever. Always call `useUiStore.getState()` inside the handler body, never outside.
- **Global capture bugs**: `preventDefault()` runs for every matched combo (`shortcuts.ts:323`). A shortcut that's too broad (e.g. plain `f`) will eat legitimate typing if the editable-field guard fails for any reason. Prefer letters that aren't bound for core typing (j/k/l/g/f/a/s/t are already consumed).
- **Missing unbind**: `useEffect` returns a cleanup function that removes the listener. If HMR reloads `shortcuts.ts` without unbinding, you get stacked listeners. Symptom: one keypress fires the handler twice. Fix: always include the cleanup — the existing code does.
