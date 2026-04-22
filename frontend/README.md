# Harmonograf frontend

React 19 + TypeScript + Vite dev server for the Harmonograf console.

## Layout

```
frontend/src/
├── main.tsx / App.tsx           Vite entry; mounts <Shell />
├── index.css                    Material Design 3 tokens + global styles
├── theme/                       Color, typography tokens
├── gantt/                       Canvas Gantt renderer (SessionStore, AgentRegistry, TaskRegistry, layout, viewport, spatial index)
├── rpc/                         Connect-RPC transport, hooks, proto converters
├── state/                       Zustand stores: ui, sessions, annotations, approvals, popover
├── lib/                         shortcuts, interventions deriver (mirrors server aggregator)
├── components/
│   ├── shell/                   Shell, AppBar, NavRail, Drawer
│   ├── Gantt/                   Minimap, context-window badges
│   ├── Interventions/           InterventionsTimeline (the #71/#76 strip)
│   ├── Interaction/             SpanPopover, steering controls
│   ├── DelegationTooltip/       Delegation edge hover cards
│   ├── TransportBar/            Live/paused indicator + controls
│   ├── LiveActivity/            Current-activity panel
│   ├── OrchestrationTimeline/   Task-stage timeline (secondary)
│   ├── SessionPicker/
│   └── TaskStages/
├── pb/                          Generated protobuf-es stubs (checked in; never edit)
└── __tests__/                   Vitest
```

## Running against a server

`pnpm dev` starts Vite on `http://127.0.0.1:5173`. The app talks
gRPC-Web to the harmonograf server's gRPC-Web listener on
`http://127.0.0.1:5174` by default (overridable via the
`VITE_HARMONOGRAF_API` env var).

```bash
# Terminal 1 — server on :7531 (native gRPC) + :5174 (gRPC-Web)
python -m harmonograf_server --store sqlite --data-dir data

# Terminal 2 — Vite dev server on :5173, talks to :5174
pnpm dev
```

Do not confuse the ports:

- `7531` — native gRPC, for agent clients only.
- `5174` — gRPC-Web, for the browser (this is what the frontend
  talks to).
- `5173` — Vite dev server (the HTML/JS/CSS the browser loads).

```bash
# Point at a non-default backend:
VITE_HARMONOGRAF_API=http://127.0.0.1:15174 pnpm dev
```

If the URL is unreachable, the session picker falls back to a
"Server unreachable — showing demo sessions" view backed by
`SessionPicker/mockSessions.ts`.

## Running the full demo

From the repo root, `make demo` boots the server, the Vite dev
server, and `adk web` with the presentation agent all at once.
That's the fastest path to a running demo. See
[`docs/dev-guide/setup.md`](../docs/dev-guide/setup.md) for
environment variables (`KIKUCHI_LLM_URL`, `USER_MODEL_NAME`,
`GOLDFIVE_EXAMPLE_PLANNER_MODEL`).

## Development

```bash
pnpm install --frozen-lockfile    # install deps
pnpm dev                          # HMR dev server
pnpm test                         # vitest one-shot
pnpm test:watch                   # vitest watch
pnpm build                        # production build into dist/
pnpm lint                         # ESLint
```

`pnpm build` runs `tsc -b` (type check) then `vite build`. The
build artifacts land in `dist/` and are not committed.

## Proto codegen

Generated stubs under `src/pb/` come from `make proto-ts` at the
repo root (uses `buf generate` against `proto/harmonograf/v1/`).
They are checked into git. Do not hand-edit.

## Architecture notes

- **Two data planes.** The canvas Gantt reads mutable stores
  (`SessionStore`, `AgentRegistry`, `TaskRegistry`) that the
  renderer reads on every animation frame. Zustand holds only
  UI-level state (selection, viewport, drawer visibility). See
  [`docs/dev-guide/frontend.md`](../docs/dev-guide/frontend.md).

- **InterventionsTimeline (#71 / #76).** The strip above the Gantt
  renders a unified intervention history derived client-side from
  annotations + drifts + plan revisions via `lib/interventions.ts`.
  Marker positioning uses a stable X anchor so hover-driven
  re-renders don't shift markers; density clustering collapses
  nearby markers into a single badge.

- **Per-agent Gantt rows (#80).** The client's
  `HarmonografTelemetryPlugin` stamps a per-ADK-agent id on every
  span. The server auto-registers a harmonograf `Agent` row for
  each distinct id on first sight. The frontend renders one row
  per agent; no special code path needed.

- **Lazy Hello (#84 / #85).** The session picker does not show a
  session until the agent actually emits its first span; importing
  the client library no longer mints ghost sessions.

## Related docs

- [`docs/user-guide/`](../docs/user-guide/) — the user-facing
  reference for every UI surface.
- [`docs/dev-guide/frontend.md`](../docs/dev-guide/frontend.md) —
  the canonical engineering guide for this package.
- [`docs/design/`](../docs/design/) — architectural notes and ADRs.
