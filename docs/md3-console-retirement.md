# Retiring the MD3 console (MD3 → zicato)

**Status:** planned · **Default flipped:** yes (the zicato console is the default as of the
`uiMode` default change; MD3 stays reachable via the `▤` toggle) · **Risk:** low (compiler-gated)

This document records the modularity analysis behind removing the legacy **Material (MD3)** console,
now that the **zicato** console is the default. It is the reference for the cleanup PR.

---

## TL;DR

The two consoles are **cleanly decoupled view trees over a shared data/state/rpc core**. The cleanup
is mechanical and low-risk because:

1. `components/zicato/**` imports **zero** MD3-UI code — verified by import-graph grep.
2. The only place that knows about both consoles is **one branch in `App.tsx`**.
3. `tsc -b` (project references + `noUnusedLocals`) is a **hard gate**: any missed coupling surfaces as
   a *compile error pointing at the exact import*, not a runtime break that ships.

Estimate: an afternoon, mechanical. The only genuine open question is *product* parity (does zicato
cover everything MD3-only the team still relies on?), not code risk.

---

## Architecture: how the two consoles relate

```
                    ┌───────────────────────── App.tsx ─────────────────────────┐
                    │  uiMode === 'zicato' ? <ZicatoConsole/> : <Shell/>         │  ← the ONLY coupling
                    │  hash '#/stress'      → <StressPage/>   (dev-only canvas)   │
                    └────────────────────────────────────────────────────────────┘
                                 │                                  │
              ┌──────────────────┘                                  └───────────────────┐
              ▼  components/zicato/**  (SVG console)                  ▼  components/shell/** + canvas (MD3)
              │                                                       │
              └──────────────────────────┬────────────────────────────┘
                                          ▼   SHARED CORE (both depend on this)
        gantt/{index.ts (SessionStore), spatialIndex.ts, types.ts}  · state/* · rpc/* · lib/* ·
        theme/agentColors · pb/* · components/SessionPicker (⌘K picker)
```

Two independent UI trees sit on the same data core. MD3 renders the gantt on a **canvas** (`gantt/`
renderer); zicato renders it as **SVG** (`components/zicato/GanttZ`) and brought its own viewport math
(`components/zicato/ganttViewport.ts`). The richer MD3 detail surfaces (SpanPopover, JudgeInvocationDetail)
were **re-implemented** in zicato (`SpanHovercardZ`, the inspector's goldfive/judge sections, `useReasoningText`)
— *not imported* — so deleting the MD3 originals does not touch zicato.

---

## Dependency facts (verified)

`components/zicato/**` reaches outside itself only for shared infrastructure:

| Imported by zicato | What it is | Keep? |
|---|---|---|
| `gantt/index` (SessionStore), `gantt/types` | the live-session data model | **keep (shared core)** |
| `state/*` (uiStore, sessionsStore, planHistory*, annotationStore) | app state | keep |
| `rpc/*` (hooks, SessionsSyncer) | gRPC-Web + watch/store wiring | keep |
| `lib/*` (thinking, goldfiveSpan, interventionDetail, interventions, format, shortcuts) | data helpers | keep |
| `theme/agentColors` | agent/synthetic color tokens | keep |
| `components/SessionPicker/SessionPicker` | the ⌘K picker | keep (shared) |

**zicato imports NOTHING from** `components/shell`, `components/Gantt`, `components/Interaction`,
`components/Interventions`, `components/DelegationTooltip`, or the `gantt/` canvas renderer. That empty
set is the whole reason this is safe.

The shared gantt core is self-contained: `index.ts → {spatialIndex, types}`, `spatialIndex → {types}`,
`types → ∅`. No canvas file is on that path.

---

## Inventory

### Keep (shared — used by zicato and/or rpc/state/lib)
- `src/gantt/index.ts`, `src/gantt/spatialIndex.ts`, `src/gantt/types.ts`  ← the data core
- `src/state/**`, `src/rpc/**`, `src/lib/**`, `src/theme/**`, `src/pb/**`
- `src/components/SessionPicker/**`
- `src/components/zicato/**`
- `src/App.tsx`, `src/main.tsx`, `src/lib/sessionRoute.ts`

### Delete (MD3-only UI — ~17,100 LOC across these dirs)
- `src/components/shell/**` — Shell, AppBar, all MD3 views (Trajectory, Graph, Activity, Notes,
  Settings, Sessions), Drawer, transport, etc. (~23 files)
- `src/components/Interaction/**` — SpanPopover, SpanContextMenu, RangeSelectionLayer, ApprovalEditor,
  GanttDomProxy, PinStrip, AttentionSnackbar (~8 files)
- `src/components/Interventions/**` — JudgeInvocationDetail (+ css), etc. (~6 files)
- `src/components/Gantt/**` — the canvas Minimap, GanttLegend, ContextWindowBadge(s), GanttPlaceholder (~6 files)
- `src/components/DelegationTooltip/**`
- `src/gantt/` **canvas renderer only** — `GanttCanvas.tsx`, `renderer.ts`, `layout.ts`,
  `contextOverlay.ts`, `colors.ts`, `driftKinds.ts`, `stages.ts`, `stress.ts`, `mockData.ts`,
  `viewport.ts`, `StressPage.tsx`  ← **NOT** `index.ts` / `spatialIndex.ts` / `types.ts`
- The **co-located tests** for the above (SpanPopover, JudgeInvocationDetail, GraphView.\*,
  TrajectoryView.\*, ActivityView, NotesView, ApprovalDrawer, Drawer.\*, CurrentTaskStrip,
  PlanRevisionBanner, InterventionsList, GoldfiveSpanDetail, and the `gantt/*` canvas tests).

---

## Two "be careful" spots (where a naïve `rm -rf` bites)

1. **`gantt/` is a mixed directory — do NOT delete the whole thing.** `index.ts` (SessionStore),
   `spatialIndex.ts`, and `types.ts` are the shared data core used by *both* renderers and by all of
   `rpc/`/`state/`/`lib/`. Delete only the canvas-renderer files listed above; keep those three.
2. **Some `lib/` modules were originally written for MD3 but are now shared** (`thinking`,
   `goldfiveSpan`, `interventionDetail`, `interventions`). Keep them — zicato consumes them. Deleting
   MD3 components will not *error* on these; at worst a couple of helpers go unused (prune later with a
   dead-code pass, e.g. `knip`).

---

## Coupling points to edit (the only code aware of "both")

1. **`src/App.tsx`** — drop the `uiMode` branch and the `#/stress` dev route; always render
   `<ZicatoConsole/>`. Remove the `Shell` and `StressPage` imports.
2. **`src/state/uiStore.ts`** — remove the `uiMode` / `setUiMode` / `toggleUiMode` machinery and
   `UI_MODE_KEY`. **Keep** `selectedSpanId`, `drawerOpen`, `selectSpan`, `closeDrawer`, the zicato theme
   state, `currentSessionId`, etc. — zicato uses them.
3. **`src/components/zicato/ZicatoConsole.tsx`** (and the MD3 `AppBar`, which is deleted anyway) —
   remove the `▤` "switch to Material console" toggle button + its handler.

---

## Recommended sequence

1. ✅ **Flip the default to zicato** (done) — gives a real bake-in with MD3 one toggle away.
2. **Bake-in period.** Watch for anything MD3-only the team still reaches for.
3. **Cleanup PR (compiler-guided):**
   1. Edit the 3 coupling points above (App branch, uiStore toggle, ZicatoConsole toggle button).
   2. Delete the MD3-only dirs/files + their co-located tests (see Inventory).
   3. `cd frontend && npx tsc -b` → it lists *exactly* any remaining import to fix. Resolve.
   4. `pnpm build` (esbuild) and `npx vitest run` → green.
   5. Optional: `knip` to prune any now-dead shared helpers; grep for `uiMode`/`md3` leftovers.

---

## Risk assessment

| Concern | Reality |
|---|---|
| "Deleting MD3 breaks zicato" | zicato imports 0 MD3-UI files → can't break it. |
| "I'll miss a hidden import" | `tsc -b` is a hard gate — a missed coupling is a *compile error*, not a runtime regression. |
| "The shared SessionStore lives under `gantt/`" | Keep `gantt/{index,spatialIndex,types}.ts`; delete only the canvas siblings. |
| "Dev tools" | `#/stress` (StressPage) is a dev-only canvas harness — removed with MD3; no user impact. |
| **Feature parity (the real one)** | Product, not code: confirm zicato covers any MD3-only surface the team relies on (e.g. Notes/Activity/Graph) before deleting them. |

---

## Appendix — reproduce the analysis

```bash
cd frontend
# (A) zicato must import NOTHING from MD3-UI areas → expect empty:
grep -rhoE "from '(\.\./)+(components/(shell|Gantt|Interaction|Interventions)|gantt/(GanttCanvas|renderer|layout|contextOverlay|colors|driftKinds|stages|viewport|StressPage|mockData|stress))[^']*'" src/components/zicato/

# (B) zicato's external deps (all shared infra):
grep -rhoE "from '\.\./(\.\./)?[A-Za-z][^']*'" src/components/zicato/ | sort -u

# (C) the shared gantt data core is canvas-free:
grep -oE "from '\./[a-zA-Z]+'" src/gantt/index.ts src/gantt/spatialIndex.ts src/gantt/types.ts
```
