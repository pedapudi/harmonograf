---
name: hgraf-add-gantt-overlay
description: Pattern for adding a new per-agent Gantt overlay — client telemetry source → proto field → server storage → bus delta → frontend renderer layer → uiStore toggle.
---

# hgraf-add-gantt-overlay

## When to use

You want to add a new per-agent time-series overlay drawn inside the Gantt lanes — examples: context window fill (already exists; see `ContextWindowSample` in `proto/harmonograf/v1/types.proto` and `bus.py:25-36` `DELTA_CONTEXT_WINDOW_SAMPLE`), token rate, error density, thinking depth, memory footprint. An overlay is a sequence of `(agent_id, timestamp, value)` samples, rendered as a sparkline or heatmap inside or above each agent's row.

If your data is span-scoped (per-tool-call), you do not want an overlay — you want a span attribute or a span kind (see `hgraf-add-proto-field`). Overlays are for continuous or sampled signals.

## Prerequisites

- `make proto` works and you understand the three-way regen flow (see `hgraf-add-proto-field`).
- You have read the Gantt renderer at `frontend/src/gantt/renderer.ts:1-97` — three canvas layers (background / blocks / overlay) with shared viewport state.
- You understand the `SessionBus` fan-out at `server/harmonograf_server/bus.py:65-120`.

## Step-by-step

### 1. Client: emit the sample

Pick where the data is produced. For LLM-centric signals, the hook is usually in `client/harmonograf_client/adk.py` around the `after_model_callback` or `before_model_callback` seams. For process-centric signals (CPU, memory), add a periodic sampler to `client/harmonograf_client/heartbeat.py` — it already runs on a timer and can piggyback sample emission onto the heartbeat tick.

Use the existing `ContextWindowSample` plumbing as the template. Grep for `ContextWindowSample` across `client/harmonograf_client/` and `server/harmonograf_server/` to see every site you will touch.

### 2. Proto: add the sample message

Edit `proto/harmonograf/v1/types.proto`:

```proto
message TokenRateSample {
  string agent_id = 1;
  google.protobuf.Timestamp at = 2;
  double tokens_per_second = 3;
  int64 window_ms = 4;
}
```

Add an entry on the telemetry-up oneof in `proto/harmonograf/v1/telemetry.proto` so clients can stream it. Add a frontend-facing wrapper in `frontend.proto` that the server pushes out on `WatchSession` updates (pattern: one of the `SessionUpdate` variants — see the existing `ContextWindowSample` field).

Run `make proto` and verify all three generation targets produced output.

### 3. Server: storage

Add a storage dataclass next to `ContextWindowSample` in `server/harmonograf_server/storage/base.py`. Add an insert method in the sqlite store (`storage/sqlite.py`). Add a new table to `SCHEMA` at `sqlite.py:45-170` (or a new column to an existing table if the signal is sparse and you want per-span attribution).

Add a `DELTA_TOKEN_RATE_SAMPLE` constant to `server/harmonograf_server/bus.py:25-36` and publish it from the ingest path (`ingest.py`) when the client sends a new sample.

### 4. Server: ingest + fan-out

In `server/harmonograf_server/ingest.py`, find the oneof dispatch in the `StreamTelemetry` handler. Add a branch for your new message type that:
1. Persists to storage (via the store method you added).
2. Publishes a `Delta` on the per-session `SessionBus` with your new `DELTA_*` kind.

The bus fan-out is already wired — `WatchSession` subscribers pick up your delta automatically as long as the `SessionUpdate` message in `frontend.proto` has a field for it. If not, add the field and add a mapping in `server/harmonograf_server/rpc/frontend_service.py` (or equivalent — grep for `WatchSession`).

### 5. Frontend: receive and store

`frontend/src/rpc/hooks.ts` and `frontend/src/rpc/convert.ts` handle the inbound stream. Add a mapping from the new pb variant to a domain type, and append samples into a new store (create `frontend/src/state/tokenRateStore.ts` following the pattern of `sessionsStore.ts:6-18`).

For high-frequency samples, keep the store size bounded — use a ring buffer keyed on `agent_id`. The existing context-window implementation should be a good template.

### 6. Frontend: renderer layer

Extend `frontend/src/gantt/renderer.ts`. There are three canvas layers (lines 1-97): background, blocks, overlay. You have two options:

**A. Overlay on the existing overlay canvas.** Preferred when the signal is a thin sparkline that should float above the blocks. Add a draw call in the overlay pass, subscribe to your new store, translate (timestamp → x) using viewport state, translate (value → y within the agent's row).

**B. New dedicated canvas.** Required if your overlay needs independent hit-testing or repaints on a different cadence. Add a fourth canvas layer, initialize it in `GanttCanvas.tsx`, and give it its own RAF loop so background repaints don't trigger yours.

**Tradeoff:** Option A is simpler but couples your repaint rate to the existing overlay (runs on every hover change — potentially wasteful). Option B is harder to wire but decouples. Start with A and migrate to B only if performance profiling shows the overlay dominating.

Hit-testing (hover tooltips for overlay samples) goes through `spatialIndex.ts` — add a new bucket for your overlay points if you want them clickable.

### 7. Frontend: uiStore toggle

Add a boolean toggle to `frontend/src/state/uiStore.ts` (pattern: `readTaskPlanVisible`/`writeTaskPlanVisible` at lines 18-89 — they persist to `localStorage`):

```ts
const TOKEN_RATE_OVERLAY_KEY = "harmonograf.ui.tokenRateOverlay";
const readTokenRateOverlay = () => localStorage.getItem(TOKEN_RATE_OVERLAY_KEY) === "1";
const writeTokenRateOverlay = (v: boolean) => localStorage.setItem(TOKEN_RATE_OVERLAY_KEY, v ? "1" : "0");
```

Add the field and setter to `UiState` in the same file. Wire a toggle into the AppBar or a view-options drawer (`frontend/src/components/shell/AppBar.tsx`). The renderer reads the flag and skips the overlay draw call when disabled.

### 8. Tests

- **Client:** round-trip test in `client/tests/test_transport_protocol.py` — assert the new message serializes and the transport mock delivers it.
- **Server:** storage round-trip test + bus delivery test (e.g. `server/tests/test_bus.py` if present, else `test_ingest.py`).
- **Frontend:** `frontend/src/__tests__/` vitest for the store ingestion + a snapshot/regression test of the renderer draw path if one exists.

## Verification

```bash
make proto
cd client && uv run --with pytest --with pytest-asyncio python -m pytest -q
cd server && uv run --with pytest --with pytest-asyncio python -m pytest -q
cd frontend && pnpm lint && pnpm build

# Live smoke:
make demo
# Toggle the overlay on/off in the UI. Drive a prompt. Confirm the overlay renders
# over each agent row with correct scaling and that the toggle persists across reloads.
```

## Common pitfalls

- **Coordinate-space bugs.** The Gantt has multiple coordinate systems: (wall-clock ms → viewport pixels), (lane index → row y), (sample value → overlay y). Every new overlay reinvents the last one; read `renderer.ts` and `viewport.ts` carefully and reuse the helpers. A 3px offset that looks fine at one zoom will be hilariously wrong at another.
- **Repaint cost.** If you naively call the overlay draw on every store update (e.g. 10Hz samples per agent × 8 agents = 80Hz), you will tank the RAF loop. Batch into the existing RAF; do not schedule your own.
- **Store unbounded growth.** A 30-minute session at 1Hz per agent is trivial; a 30-minute session at 100Hz per agent × 8 agents is 1.4M samples. Use a ring buffer. See `client/harmonograf_client/buffer.py` for a pattern.
- **Backpressure.** The bus drops samples under load (see `Subscription.dropped` counter at `bus.py:51-63`). Your overlay will have holes; do not draw fake zeros — render gaps.
- **SessionBus delta kind collision.** Pick a `DELTA_*` constant name that does not collide with existing ones (`bus.py:25-36`). The protocol is string-keyed; collisions fail silently.
- **uiStore persistence key.** If you reuse an existing localStorage key prefix, you will silently read the old value as your new toggle. Use a dedicated, specific key.
- **Gantt row height changes.** If your overlay needs vertical space, you will be tempted to grow the row height. Row heights are defined near `renderer.ts:55-96` and are used by the spatial index, the minimap (`frontend/src/components/Gantt/Minimap.tsx`), and the hover hit-testing. Changing them has ripple effects — audit all three before committing.
- **Missing schema version bump.** If you added a sqlite table, old databases will not have it. Follow the migration guidance in `hgraf-add-proto-field` step 5 — `ALTER TABLE` / `CREATE TABLE IF NOT EXISTS`, never `DROP`.
