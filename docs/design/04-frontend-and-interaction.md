# 04 — Frontend & Human Interaction Model

Status: **DRAFT — awaiting review**
Scope: the Gantt console. What the user sees, how they interact, how it renders fast, what colors mean.

This doc presupposes the span/session/control model defined in `01-data-model-and-rpc.md`. If that model changes, this doc changes with it.

---

## 1. Design goals

1. **Glanceable state.** A user opening the console should know within one second: how many agents are running, whether anything needs their attention, what the current moment looks like.
2. **Coordination, not just observation.** Every piece of visual information has a corresponding interaction: hover to peek, click to inspect, right-click to act. Read-only views are a failure mode.
3. **60fps always.** Interaction never stutters. A session with 10,000+ spans pans and zooms at full framerate. This constrains every rendering choice below.
4. **Material Design 3 as the design language.** Typography, spacing, motion, state layers, and color roles follow MD3. The Gantt is custom, but its chrome, controls, drawers, and dialogs are MD3-native.
5. **Color is a first-class communication channel.** Not decoration. A user should be able to tell kind × status from color alone, across the full zoom range, with dark-mode defaulted and light-mode supported.

---

## 2. Stack

- **React 18 + TypeScript**. Concurrent rendering used for drawer/inspector updates only; hot Gantt rendering bypasses React.
- **Gantt renderer: custom Canvas2D**, with a WebGL/PixiJS upgrade path reserved if profiling shows Canvas2D can't hit the 10k-block target on mid-range hardware. Existing DOM-based Gantt libraries (vis-timeline, frappe-gantt, dhtmlx) are rejected: none sustain 60fps above ~1k DOM nodes, and we need an order of magnitude more.
- **State**: Zustand for UI state (selected span, drawer open/closed, zoom, filters). Plain mutable stores for hot data (the span index) — subscribed to by the canvas renderer directly, *not* through React.
- **MD3 chrome**: `@material/web` for app bar, navigation rail, dialogs, menus, buttons, text fields, snackbars.
- **RPC**: gRPC-Web via `grpc-web` or `connect-web`, talking to the Python server. If gRPC-Web proxy friction bites, fallback is WebSocket with a thin framing layer — the protocol is the same, only transport changes.
- **Build**: Vite.

---

## 3. Layout

```
┌─────────────────────────────────────────────────────────────────────────┐
│  [☰]  Harmonograf     [Session picker ▾]       [🔔 2] [⚙] [👤]         │  ← App bar (MD3)
├───┬─────────────────────────────────────────────────────────┬───────────┤
│   │                                                         │           │
│ N │                                                         │  Drawer   │
│ a │                                                         │ (closed   │
│ v │                    Gantt chart                          │ unless a  │
│   │                                                         │ span is   │
│ r │                                                         │ selected) │
│ a │                                                         │           │
│ i │                                                         │           │
│ l │                                                         │           │
│   ├─────────────────────────────────────────────────────────┤           │
│   │  Minimap (full session at a glance)                     │           │
│   ├─────────────────────────────────────────────────────────┤           │
│   │  [⏮]  [⏸]  [▶]  [⏭]  [⏹]   0:03:24 / LIVE    [-] [+]   │           │
└───┴─────────────────────────────────────────────────────────┴───────────┘
```

- **Navigation rail (left, MD3)**: Sessions, Activity queue (needs attention), Annotations, Settings.
- **App bar (top)**: Session picker combobox, notification bell with attention count, dark/light toggle, user menu.
- **Main area**: Gantt (top ~75%), minimap (thin strip), transport bar (bottom).
- **Drawer (right, MD3 modal side-sheet)**: opens on span selection. Wide enough for payload inspection without wrapping (min 480px, resizable).
- **Responsive**: below 1200px wide, drawer becomes a bottom sheet. Below 800px, navigation rail collapses to a hamburger. Mobile is explicitly deprioritized — this is a desktop operator console.

---

## 4. Session picker

- Click the app bar picker → MD3 menu with search + sections.
- Sections:
  - **Live** — green status dot, pulsing. Sorted by last activity descending.
  - **Recent** — completed in the last 24h.
  - **Archive** — older, behind a "Show all" expander. Paginated — the picker never lists more than 50 at once.
- Each row: title, agent count, duration, last activity time, attention badge if any span is `AWAITING_HUMAN`.
- Keyboard: `⌘K` / `Ctrl+K` opens the picker with focus on search.
- Switching sessions keeps the drawer state per-session (reopening a session reopens its last-selected span).

---

## 5. The Gantt chart

### 5.1 Axes & rows

- **X-axis: time.** Scales from 30s window (high-detail) to 6h window (overview). Tick granularity adapts: 1s ticks at 30s zoom, 5s at 1min, 15s at 5min, 1min at 30min, 5min at 2h, 15min at 6h.
- **Y-axis: agents.** One primary row per agent. Agents sorted by join time ascending (stable ordering — a new agent always appears at the bottom, existing rows don't shuffle).
- **Nested spans**: within an agent row, nested spans stack in sub-lanes. The parent `INVOCATION` is a thin band at the top of the row; child `LLM_CALL`s and `TOOL_CALL`s occupy sub-lanes below. Sub-lane layout uses a greedy interval-packing algorithm: each new span takes the first non-overlapping lane.
- **Row height**: default 56px (MD3 list item), expandable to 120px when the row is "focused" (user clicks the row header) to show more sub-lanes and longer labels.

### 5.2 Blocks (spans)

A span renders as a rounded rectangle (MD3 shape tokens: `small` — 8px corners). At high zoom the block shows:

- Left-aligned icon (span kind)
- Name (truncated with ellipsis)
- Right-aligned duration if space permits

At low zoom (block width < 24px), only the fill color is drawn — no icon, no text. This is critical for perf and glanceability: a 6-hour session with thousands of sub-second tool calls becomes a color band, not a label-collision disaster.

**Minimum visual width**: 2px. Sub-pixel blocks get merged into a "density stripe" per sub-lane with a count badge (e.g., "×37") on hover.

### 5.3 The "now" cursor and auto-follow

- Vertical line at the current wall-clock time, MD3 `primary` color, 2px.
- In **live mode**, the viewport auto-scrolls to keep the cursor at 80% of the visible width (leaves room for upcoming PLANNED spans to render ahead).
- Any user pan or zoom breaks auto-follow. A persistent MD3 floating action button appears in the bottom-right: **"Return to live"** — click it to resume following.
- In **replay mode** (historical session), no cursor; the user freely navigates.

### 5.4 Zoom & pan

- **Zoom**: mouse wheel (with trackpad pinch), keyboard `+`/`-`, or the transport bar zoom buttons. Zoom centers on cursor position, not viewport center. Range is hard-clamped to [30s, 6h].
- **Pan**: click-and-drag on empty timeline area. Middle-mouse-drag anywhere. Shift+wheel for horizontal pan without zoom. Keyboard arrows for 10% steps.
- **Scrub**: drag the "now" cursor (in replay mode only) to scrub through time; the drawer updates live with the span under the cursor.

---

## 6. Color — the communication layer

Color encodes **two independent dimensions simultaneously**: span **kind** (what) and **status** (how it's going). Plus error and attention states as overrides.

### 6.1 Dimension one: kind (hue)

Drawn from MD3 color roles so it integrates cleanly with the rest of the UI:

| Kind | MD3 role | Dark mode example | Why this hue |
|---|---|---|---|
| `INVOCATION` | `surface-variant` | muted neutral | It's the container; should recede behind its children. |
| `LLM_CALL` | `primary` | MD3 blue | The "model thinking" — primary brand color, most common event. |
| `TOOL_CALL` | `tertiary` | MD3 teal/purple | Action into the world, visually distinct from LLM thinking. |
| `USER_MESSAGE` | `secondary` | MD3 green | Human input — positive/inviting. |
| `AGENT_MESSAGE` | `secondary-container` | lighter green | Related to user input visually, but clearly inter-agent. |
| `TRANSFER` | `inverse-primary` on a highlighted background | amber/gold | Handoff — eye-catching, tied into cross-agent arrow rendering. |
| `WAIT_FOR_HUMAN` | `error-container` | red-orange | Maximum urgency. Demands attention. |
| `PLANNED` | Same kind color at 30% opacity with dashed outline | ghost | Clearly "not yet real". |
| `CUSTOM` | `outline` | neutral gray | Generic fallback. |

### 6.2 Dimension two: status (treatment)

Applied as a treatment **on top of** the kind color:

| Status | Treatment |
|---|---|
| `PENDING` | Kind color at 40% opacity, no border. |
| `RUNNING` | Kind color at 100%, plus a subtle 2-second breathing animation (opacity 85% ↔ 100%). |
| `COMPLETED` | Kind color at 100%, solid. |
| `FAILED` | Kind color replaced by `error` red; a small warning icon in the top-right corner of the block. |
| `CANCELLED` | Kind color desaturated to 30%, with a diagonal hatch overlay. |
| `AWAITING_HUMAN` | Kind color overridden by `error-container`; 1-second pulse animation; glow outline. Always visually urgent regardless of zoom level. |

The breathing/pulsing animations are rendered at a cost ceiling: only spans in the current viewport and in `RUNNING` or `AWAITING_HUMAN` state animate. A session with hundreds of running spans stays below 16ms/frame because animation work scales with visible state count, not total state count.

### 6.3 Dimension three: agent identity (subtle)

Every agent row has a left-edge colored strip (8px wide, full row height), in the agent's assigned color. Agent colors come from a perceptually-uniform categorical palette (think `d3-scale-chromatic`'s `schemeTableau10` adapted to MD3). The strip is the *only* place agent identity is colored — the blocks themselves encode kind + status, not agent. This keeps the blocks consistent across agents: a blue LLM call looks the same on every row.

### 6.4 Accessibility

- **All color pairs pass WCAG AA contrast** against their backgrounds in both dark and light themes.
- **Color is never the only channel** for critical information:
  - `FAILED` adds an icon, not just red.
  - `AWAITING_HUMAN` adds pulse + icon + toast + queue entry.
  - Kind is conveyed by icon too, not just hue, whenever the block is wide enough.
- **High-contrast theme** available in settings: saturates all kind hues and adds outlines on every block.
- **Color-blind modes** (deuteranopia, protanopia, tritanopia): alternate palettes swap in, keeping perceptual distance between kinds.

### 6.5 What colors are NOT used for

- Not for encoding time or duration. Time is the X-axis; overloading color loses glanceability.
- Not for priority beyond `AWAITING_HUMAN`. If we need "important" vs. "normal" later, we'll use elevation or badges, not more hues.
- Not for user tags/labels. Annotations use pins above the timeline, not span recoloring, so user customization never competes with built-in semantics.

---

## 7. Interaction model

This is the heart of the doc. Every interaction maps to one or more control events or annotations from the protocol.

### 7.1 Observation — passive, fast, rich

- **Hover a block** → 200ms-delayed tooltip: kind, name, duration, status, agent, summary preview (from `payload_ref.summary`). No network call.
- **Hover an agent row header** → all of that agent's blocks highlight; other rows dim to 40% opacity. Releases on mouse-out.
- **Click a block** → opens the drawer with the full inspector (§8). Selection is persistent until another block is clicked or `Esc` pressed.
- **Shift+click** → multi-select for comparison. Drawer splits into two panes.
- **Hover a cross-agent link arrow** → both endpoints highlight, all unrelated blocks dim.

### 7.2 Annotation — lightweight capture

- **Right-click on empty timeline area** → "Add note at time T" — creates a `COMMENT` annotation bound to an agent row at time T.
- **Right-click on a block** → context menu with: Inspect, Annotate, Steer, Approve, Reject, Rewind to here, Copy link, Copy as text.
- **Annotate** opens an inline MD3 text field anchored to the block. Enter saves. Annotation appears as a colored pin above the timeline at that block's start time.
- **Pins are never rendered inside blocks** — they sit in a dedicated strip above each agent row so they survive any block size.

### 7.3 Live steering — annotation that talks back

- From the block context menu: **Steer** — opens a text field pre-seeded with "Consider: ". On enter, the annotation is created with `kind=STEERING` and pushed to the agent as a `STEER` control event.
- Steering is only enabled if the target agent's capabilities include `STEERING`. Otherwise the menu item is greyed out with a tooltip explaining why.
- Delivered steering annotations render with a checkmark on their pin once ack'd; failures render red with a retry option.

### 7.4 Human-in-the-loop approvals

When any span enters `AWAITING_HUMAN`:

1. The block pulses with `error-container` color, glow outline.
2. An MD3 snackbar rises at the bottom: "Agent X needs your input: approve `search_web(query='...')`?" with inline Approve / Reject / Edit buttons.
3. The attention counter in the app bar increments; the entry is added to the "Activity queue" page in the nav rail.
4. If the user clicks the block, the drawer opens to the approval pane (§8.3), which has the full tool args, rationale (from the LLM call that proposed it, linked), and Approve / Reject / Edit & Approve buttons.

Resolution sends a `APPROVE` or `REJECT` control event with optional edited args. The span transitions `AWAITING_HUMAN → RUNNING` when ack'd; the pulse stops.

### 7.5 Control — the transport bar

The bottom transport bar is the coarse, cross-agent control surface.

| Button | Action | Target | Constraint |
|---|---|---|---|
| ⏮ Rewind | Prompt for target: either selected block or minimap click | Agents in scope | Requires `REWIND` capability |
| ⏸ Pause | Send PAUSE | All agents in session | `PAUSE_RESUME` capability |
| ▶ Resume | Send RESUME | All paused agents | `PAUSE_RESUME` capability |
| ⏭ Step | Resume until next span boundary | Primary agent | Only in pause state |
| ⏹ Stop | Send CANCEL | All agents | `CANCEL` capability |

For **per-agent** control (pause just one agent), click the agent row header — a popover appears with the same buttons scoped to that agent.

**Rewind flow**:
1. User clicks ⏮ or picks "Rewind to here" on a block.
2. Modal confirms the target span and lists what will be discarded (downstream spans across all affected agents).
3. On confirm, server sends `REWIND_TO` to every affected agent in dependency order.
4. New spans link back to discarded ones via `REPLACES` — history is preserved; the timeline dims replaced spans to 30% opacity and the new run renders fresh alongside them.

### 7.6 Cross-agent coordination view

- **Span links render as Bezier arrows** between source and target blocks, in the `TRANSFER` amber/gold color (matching the kind).
- **Arrows are culled aggressively**: rendered only for blocks in viewport, or for links involving the currently selected/hovered block.
- **Hover a transfer arrow** → both endpoints highlight, a tooltip shows the relation (`INVOKED`, `WAITING_ON`, etc.), other unrelated blocks dim.
- **Right-click a transfer arrow** → Intercept options (if the source agent has `INTERCEPT_TRANSFER` capability): block, reroute to a different agent, delay, inject message to destination before resume.
- **Upstream/downstream graph mode**: select a block, press `G`, and the Gantt dims all blocks not in the selected block's ancestor or descendant graph. Useful for answering "what caused this?" and "what did this cause?" across agents.

### 7.7 Keyboard shortcuts

| Key | Action |
|---|---|
| `⌘K` / `Ctrl+K` | Session picker |
| `Space` | Toggle pause (all agents) |
| `←` `→` | Pan 10% |
| `+` `-` | Zoom in / out |
| `F` | Fit entire session to viewport |
| `L` | Jump to live cursor / return to live |
| `G` | Graph mode on selected span |
| `A` | Annotate selected span |
| `S` | Steer selected span |
| `Esc` | Close drawer, clear selection |
| `1`…`9` | Jump to agent row N |

All shortcuts are re-mappable in settings.

---

## 8. The inspector drawer

Opens on span selection. Tabbed.

### 8.1 Overview tab

- Breadcrumb: `session › agent › parent span › this span`
- Header: kind icon, name, status badge, duration
- Timing waterfall: mini Gantt of this span's children
- Linked spans: compact list grouped by relation, with click-to-jump
- Metadata table: all `attributes`, sortable, searchable

### 8.2 Payload tab

- Lazy-loaded via `GetPayload`. Shows a loading skeleton until bytes arrive.
- Renders by mime:
  - `application/json` → collapsible tree viewer with search
  - `text/*` → monospace viewer with syntax highlighting (Shiki)
  - `image/*` → inline image
  - binary → hex dump with ASCII sidebar
- Copy-to-clipboard button. Download button. Diff-vs-other-span button (if two spans are multi-selected).
- If the payload was evicted client-side, shows "Payload was not preserved (client under backpressure)" with a retry button (sends `PayloadRequest` upstream).

### 8.3 Approval tab (only for `AWAITING_HUMAN` spans)

- Big Approve / Reject / Edit & Approve buttons at the top.
- The proposed action rendered clearly (tool name, args formatted by mime).
- Rationale: the parent `LLM_CALL`'s completion, showing why the agent wants to do this.
- Free-text field for a rejection reason (required for Reject).
- Keyboard: `A` approve, `R` reject, `E` edit.

### 8.4 Annotations tab

- All annotations on this span, chronologically.
- Inline compose field at the bottom.

### 8.5 Raw tab

- Unformatted JSON of the span record, for debugging.

---

## 9. Performance architecture

Hitting 60fps with 10k+ blocks requires discipline, not tricks. The key moves:

### 9.1 Render loop architecture

Three canvas layers, each redrawn only when its dependencies change:

1. **Background layer**: rows, gridlines, time markers, agent color strips. Redraws on zoom, pan, or agent list change. Typical: 1–2 redraws per second of active interaction.
2. **Blocks layer**: all span rectangles. Redraws on viewport change or span data change. This is the hot layer. Optimized with:
   - Pre-bucketed spatial index (interval tree per agent row, rebuilt incrementally as spans arrive)
   - Viewport culling via tree query — never iterates all spans
   - Single `fillRect` batch pass per color bucket (all blue blocks drawn in one pass, then all teal, etc.) to minimize canvas state changes
3. **Overlay layer**: hover highlight, selection, cursor, cross-agent arrows, pulse/breathe animations. Redraws on every frame that has any animated state, otherwise on interaction only.

Layer separation means pan-without-data-change only redraws background + blocks (skipping overlay animation cost), and live-data-arrival without viewport change only redraws blocks (skipping background).

### 9.2 Data path: protocol → render

- `WatchSession` server stream → raw gRPC-Web message decoder (runs on main thread, but cheap: protobuf parse is microseconds per message at this volume)
- Decoded span → mutable span index (Map<id, Span> + per-agent interval tree)
- Mutation notifies the renderer via a simple dirty-rect system: "the region between time T1 and T2 on agent A needs redraw next frame"
- Renderer coalesces dirty rects each `requestAnimationFrame` and redraws only the dirty region of the blocks layer

**React never re-renders the Gantt on data changes.** React only re-renders chrome (app bar, drawer, nav rail) on user interaction. This is non-negotiable for perf.

### 9.3 Budgets

| Metric | Target | How measured |
|---|---|---|
| Frame time (pan/zoom) | < 16ms | Performance observer, p95 |
| Live event → visible | < 100ms | Client-side timestamps on ingest |
| Session open → first paint | < 500ms for 1k-span session | TTI mark |
| Drawer open → payload rendered | < 300ms for 1MB payload | Custom mark |
| Memory per session | < 200MB at 10k spans | `performance.memory` sampling |

These are hard budgets. Regressions fail CI.

### 9.4 Stress scenarios

The perf test suite includes:
- **Steady state**: 5 agents, 10 spans/sec each, 2 hours live → 360k spans total
- **Burst**: 1 agent emitting 500 spans/sec for 10 seconds
- **Big payloads**: 50 LLM calls each with 1MB completions
- **Cross-agent chatter**: 10 agents transferring between each other every 200ms

If any scenario drops below 60fps, it's a bug.

---

## 10. MD3 compliance notes

- **Color tokens**: all colors reference MD3 semantic roles (`primary`, `on-primary`, `surface`, `error`, etc.) — no raw hex in component code. Palette generated via Material Color Utilities from a single seed color.
- **Typography**: Roboto Flex via variable font; MD3 type scale (`display-*`, `headline-*`, `title-*`, `body-*`, `label-*`). Gantt block labels use `label-small` (11sp).
- **Motion**: MD3 easing tokens (`standard`, `emphasized`). Block pulse animation uses `emphasized-decelerate` for the brighten-in, `emphasized-accelerate` for the fade-out.
- **Elevation**: drawer uses level 2, menus use level 3, dialogs use level 3. Gantt canvas is flat (level 0) except for the hovered block which gets a level-1 drop shadow.
- **Shapes**: MD3 shape tokens — `small` (8px) for blocks, `medium` (12px) for drawer, `large` (16px) for dialogs, `full` for FABs.
- **State layers**: hover, focus, pressed, selected all use MD3 state-layer opacities over the base color.
- **Components used**: `@material/web`'s `md-top-app-bar`, `md-navigation-rail`, `md-filled-button`, `md-outlined-button`, `md-text-button`, `md-icon-button`, `md-filled-text-field`, `md-menu`, `md-dialog`, `md-snackbar`, `md-list`, `md-divider`, `md-fab`.

---

## 11. Open questions for your review

- **G**: For rewind, should we dim replaced spans to 30% opacity (preserves visual history) or hide them by default with a "show replaced" toggle (cleaner but hides state)?
- **H**: Steering annotations — should they appear on the timeline as pins (persistent, reviewable after the fact) or only as ephemeral events rendered briefly at the moment of delivery?
- **I**: In graph mode, should non-graph blocks be dimmed (current plan) or completely hidden? Dimming preserves context; hiding maximizes focus.
- **J**: Should there be an "edit and approve" inline UI for tool calls, or just free-text editing of JSON args? Inline is nicer for common tool types but requires per-tool schemas.
- **K**: Per-agent row colors: should the user be able to override them (pinning colors to specific agents), or are they system-assigned for consistency?
- **L**: Dark mode default — agreed — but should there be an AMOLED / true-black variant for OLED panels?

---

## 12. Out of scope for this doc

- Anything behind the server API (fanout, persistence, control routing) → `03-server.md`
- Anything inside the agent process (emit API, ADK hooks, buffering internals) → `02-client-library.md`
- The span schema itself → `01-data-model-and-rpc.md`
