# Keyboard shortcuts

Every shortcut in the frontend is defined in one place:
`frontend/src/lib/shortcuts.ts`. The table below mirrors that file
verbatim. If a shortcut isn't in this table, it doesn't exist.

## Modifiers

Combos are matched case-insensitively. The `mod+` prefix means **Cmd**
on macOS, **Ctrl** elsewhere — the key handler auto-detects the
platform.

Shortcuts fire on any focused window element **except** inputs and
textareas (including Material Web's `md-filled-text-field` /
`md-outlined-text-field`). Two keys are allowed through editable fields:

- `Esc` — always closes whatever overlay is open.
- `⌘K` / `Ctrl+K` — opens the session picker even from an input.

## The map

| Key | Action | Notes |
|---|---|---|
| `⌘K` / `Ctrl+K` | Open session picker | Aliased to `/` below. |
| `/` | Open session picker (aliased to search) | |
| `Space` | Toggle live-follow | Also stops pausing the viewport when on. Pause *agents* is on the transport bar. |
| `←` | Pan 10% left | **Stub** — handler reserved, wiring lands in task #11. |
| `→` | Pan 10% right | **Stub** — same. |
| `+` | Zoom in | |
| `=` | Zoom in | Alias for `+` so US keyboards don't need Shift. |
| `-` | Zoom out | |
| `f` | Fit session to viewport | Resets `zoomSeconds` to 3600 (1 hour). |
| `l` | Return to live cursor | Same as the transport bar's **↩ Follow live** button. |
| `a` | Annotate selected span | **Stub** — handler reserved, wiring lands in task #14. Use the [drawer](drawer.md#annotations-tab) or the [popover](drawer.md#span-popover) until then. |
| `s` | Steer selected span | **Stub** — same. Use the [popover](control-actions.md#1-span-popover-quick-look) or the [drawer Control tab](drawer.md#control-tab) until then. |
| `j` | Select next span | Spans are sorted by `(startMs, id)` across all agents. |
| `k` | Select previous span | |
| `[` | Focus previous agent row | |
| `]` | Focus next agent row | |
| `g` | Jump to first agent row | |
| `⇧g` | Jump to last agent row | |
| `⇧?` | Toggle keyboard help overlay | |
| `Esc` | Close overlay / clear selection | Resolves in order: help overlay → session picker → drawer. |

## Help overlay (`?`)

Press **⇧?** to open the help overlay. It's a compact cheatsheet of the
table above, useful when you forget a combo but remember the general
shape. `Esc` or `?` closes it.

The help overlay is driven by a separate shortcut list in
`HelpOverlay.tsx`, so in principle it could drift from the
`shortcuts.ts` source of truth. If you notice a mismatch, the code is
authoritative — file it as a bug.

## The `j`/`k` traversal order

The `j`/`k` shortcuts walk spans in a **single global order** across all
agents:

1. For each agent in the session, list every span.
2. Sort by `startMs` ascending, breaking ties by span id.
3. `j` moves forward, `k` moves backward.

The traversal does **not** wrap at the ends — hitting `k` on the first
span is a no-op. It also does **not** respect agent focus, so `j`/`k`
may cross agent rows.

If you want to walk one agent's spans in isolation, use `[` / `]` to
narrow focus first, then click to start — span navigation within a
focus is a future affordance, not a current one.

## Shortcuts *not* in the global map

A few key combos exist but live inside specific components:

- The **steer editor** (inside the [span popover](control-actions.md))
  sends on `⌘↵` / `Ctrl+↵` and cancels on `Esc`.
- The **session picker search field** uses `Esc` to close and plain
  typing to filter.
- Material Web text fields swallow arrow keys and most other keys — this
  is expected and the global handler explicitly lets typing through
  until you tab out.

## Related pages

- [Control actions](control-actions.md) — for what `s` will eventually do.
- [Annotations](annotations.md) — for what `a` will eventually do.
- [Gantt view](gantt-view.md#navigating--pan-zoom-and-selection) — for the bigger picture on viewport navigation.
