# Zicato console — frontend port: state & resume guide

**Last updated:** 2026-06-13 · **Branch:** `design/sequence-compose-parity` · **Status:** ⚠️ **first cut — builds & passes CI, NOT yet verified against real data. Do not consider production-ready.**

This document is the single place to resume work on porting the **zicato-language UI
study** into the real React frontend as a parallel, toggle-able console. It covers
what exists, what's verified vs. not, how to run it for real, the architecture, and a
file-level map with code pointers.

---

## 1. TL;DR

harmonograf's production console (`frontend/`) is a React + TypeScript + Vite app built
on `@material/web` (MD3), `zustand`, and connectrpc. A separate **design study** in
`docs/design/zicato-ui-study/` redraws that console in "zicato's design language"
(Tufte/line-art, monospace-forward, single-accent-on-calm-ground, theme-switchable).

This work **ports that study into the real app as a second, parallel console** that the
user can toggle to/from the MD3 one:

- A new `uiMode: 'md3' | 'zicato'` flag (persisted, **default `md3`**) branches the app
  at the top level. Production behaviour is unchanged until a user opts in.
- The zicato console is a full parallel component tree under
  `frontend/src/components/zicato/`, reading **real session data** (the same RPC
  hooks/zustand stores the MD3 console uses) through a dedicated **adapter** that maps the
  live session model into the study's render inputs.
- All six study figures were ported to React: hero **gantt**, topology **chord**, drift +
  judge **seismograph**, intervention **ladder**, **plan** reel→DAG, vertical **sequence**
  diagram, and the session **fingerprint**.

It compiles, lints, the full test suite is green (740/740), and both consoles mount in a
headless browser. **It has not been rendered against a live session** — that is the
critical, not-yet-done verification step (see §3 and §8).

---

## 2. Status at a glance

| Area | State |
| --- | --- |
| `pnpm build` (`tsc -b && vite build`) | ✅ passes |
| `pnpm lint` (eslint) | ✅ clean |
| `pnpm test` (vitest) | ✅ 740/740 (incl. 6 zicato smoke tests) |
| Empty-state mount (headless chrome, no server) | ✅ both consoles render, no crash, toggle present |
| **Render against a live session (real data)** | ❌ **never done** — the adapter mappings + every figure are unexercised against real RPC data |
| Interactive QA (span→inspector, chord hover/pin, reel→DAG, ⌘K, theme switch, toggle round-trip) | ❌ not exercised with content |
| Committed to git | ✅ (on `design/sequence-compose-parity`; not merged) |
| Affects the live MD3 UI | ❌ no — gated behind a default-off toggle |

The 6 smoke tests (`frontend/src/__tests__/components/zicato.smoke.test.tsx`) assert the
console **renders against an empty store without throwing** and that token/agent mappers
are correct. They are a scaffold gate, **not** a correctness check of the figures.

---

## 3. How to run it for real (the most important next step)

The figures have only ever been seen empty. To validate them you need a **live session**.
Two paths:

### A. Full stack (server serves the built SPA)

```bash
# 1. build the console into the server's static dir
make console                      # → harmonograf_server/_console (or frontend/dist)

# 2. run the server (serves gRPC-Web + the console SPA on the web port)
make server-run                   # or: python -m harmonograf_server  (see server/harmonograf_server/cli.py)

# 3. drive an orchestration to produce a live session
ls examples/                      # → standalone_observability
#    run the example so spans/plans/drift stream into a session

# 4. open the served console in a browser, then flip to zicato:
#    AppBar ⧉ button (top-right), OR set localStorage 'harmonograf.uiMode'='zicato'
```

### B. Vite dev server pointed at a running backend

```bash
make server-run                                          # backend on its web port (e.g. 127.0.0.1:17532)
VITE_HARMONOGRAF_API=http://127.0.0.1:17532 make frontend-dev   # vite dev w/ HMR
# transport.ts resolves: window.__HARMONOGRAF_API__ → VITE_HARMONOGRAF_API → compiled default
```

Then, **in either case**, walk every figure against real data and fix what's wrong:
gantt spans/lanes/now-line/transfers, chord projection, seismograph drift + judge beats,
ladder, plan reel→DAG, sequence diagram, fingerprint, span→inspector, ⌘K session switch,
and all 16 zicato themes. The adapter (`adapter.ts`) is where most real-data bugs will be.

> The toggle persists in `localStorage['harmonograf.uiMode']`. To force the zicato console
> for a screenshot without clicking, seed that key before the bundle loads.

---

## 4. Architecture

### 4.1 The toggle (md3 ↔ zicato)

The whole feature hangs off one persisted store flag; nothing else about the MD3 app changes.

- **Store** — `frontend/src/state/uiStore.ts`
  - `UI_MODE_KEY = 'harmonograf.uiMode'` (L83), `ZICATO_THEME_KEY = 'harmonograf.zicatoTheme'` (L84)
  - `uiMode` / `setUiMode` / `toggleUiMode` in the `UiState` interface (L367–369), plus `zicatoTheme` (L370+)
  - Initial values + reducers: `uiMode: readUiMode()` (L536), `setUiMode`/`toggleUiMode` (L538–547), `zicatoTheme: readZicatoTheme()` (L548). Persistence uses the file's existing per-key `read*`/`write*` helper pattern. **Default is `md3`.**
- **Branch** — `frontend/src/App.tsx` L48–51: `const uiMode = useUiStore(s => s.uiMode)`; `#/stress` is checked first (dev route unaffected), then `uiMode === 'zicato' → <ZicatoConsole/>`, else `<Shell/>`. The session deep-link hook stays **above** the branch so links work in both consoles.
- **Toggle controls (both directions):**
  - MD3 → zicato: `frontend/src/components/shell/AppBar.tsx` L73–81 — `⧉` icon button, `onClick={() => setUiMode('zicato')}`, `data-testid="ui-mode-toggle"`.
  - zicato → MD3: `ZicatoConsole.tsx` topbar — `data-testid="ui-mode-toggle-z"` (L205), wired via `onToMd3={() => setUiMode('md3')}` (L523).

### 4.2 The console shell

`frontend/src/components/zicato/ZicatoConsole.tsx` (538 lines) ports `compose.html`'s
`appHybrid` shell. The top-level component is `ZicatoConsole()` (L508):

```
<div className="zk-root" data-zicato-theme={zicatoTheme}>      // L520 — theme scoped here
  <SessionsSyncer/>                                            // session list poll (reused from MD3)
  <ZicatoTopbar z onToMd3/>            // brand · session crumb (⌘K) · status · theme picker · ⧉→md3
  <ZicatoStrip z/>                     // goal + live/done/failed pill
  <div className="zk-app-body">
    <ZicatoRail view onView/>          // rail: 'gantt' | 'instruments'
    <main className="zk-main">{ view==='gantt' ? <GanttViewZ/> : <InstrumentsViewZ/> }</main>
    <ZicatoInspector/>                 // docked span/session drawer
  </div>
  <ZicatoTransport/>                   // transport bar (live clock)
  <SessionPicker/>                     // ⌘K picker — REUSED from MD3
</div>
```

Sub-components in the same file: `ZicatoThemePicker` (L110), `ZicatoTopbar` (L163),
`statusPill` (L215), `ZicatoStrip` (L221), `ZicatoRail` + `ZicatoView` type (L247/L249),
`ZicatoTransport` (L279), `ZicatoInspector` (L387). View selection is **local React state**
(`useState<ZicatoView>('gantt')`, L518) — not in the store. `useSessionWatch(sessionId)`
(L516) keeps one stream alive for all sub-views; `useGlobalShortcuts()` (L509) is owned here.

### 4.3 Data flow — the adapter (the linchpin)

`frontend/src/components/zicato/adapter.ts` (1147 lines) is the boundary between the real
session model and the study's render inputs. **All real-data correctness lives here.**

- **Public hook:** `useZicatoSession(sessionId): ZSession` (L1012). It:
  - gets the live store via `getSessionStore(sessionId)`;
  - is **reactive**: subscribes to every registry (`spans`, `agents`, `tasks`, `drifts`,
    `delegations`, `contextSeries`) plus a 1s timer, snapshotting `Date.now()` in a reducer
    (not render) so the play-head/`now` advances (L1020–1041);
  - pulls goal/title from `useSessionsStore`, annotations from `useAnnotationStore`, plan
    inputs from `usePlanHistory` / `useCumulativePlan` / `useSupersedesMap`, and
    `trajectorySelectedPlanId` / `selectedRevision` from `useUiStore`;
  - returns `EMPTY_SESSION` when there's no store, or a graceful partial when the session
    has no spans/agents yet (L1062–1067) — **figures must never crash on empty/loading.**
- **Output type:** `ZSession` (L191) — `{ id, goal, status, T, now, agents[], spans[], transfers[], delegation, edges[], judges, ticks, ladder, ctx, plan, fp, empty }`.
- **Fallback:** `EMPTY_SESSION` (L239, `empty: true`, all arrays empty, `T: 30`, `now: 0`).
- **Builders** (each maps a slice of `SessionStore`): `buildAgents` (L373), `buildSpans`
  (L419), `buildTransfers` (L450), `buildDelegation` (L465), `buildEdges` (L496, derives
  transfer/delegation/return edges — same algo as MD3 GraphView), `buildJudges` (L623),
  `buildTicks` (L642), `buildLadder` (L664), `buildCtx` (L703), `buildPlan` (L735),
  `deriveFingerprint` (L947), `deriveStatus` (after L1147).
- **Encoding mappers:** `toKindToken` (L285, `LLM_CALL→'llm-call'` …), `toStatusToken`
  (L311, `AWAITING_HUMAN→'awaiting'` …), `gfClassForSpan` (L335), `colorVar` (L994,
  agent → `--hg-agent-*` token), `severityToValue` (L269, goldfive drift severity → value).

`svgUtils.ts` (140 lines) holds the shared render helpers ported from the study:
`KIND` (L13), `gfVar` (L16), `statusFill` (L26), `lerpKeys` (L36), `lcg` (L55, seeded RNG),
`timeScale` (L65, the shared `padL/padR/X` so the seismograph + heartbeat + ladder share an
axis), `uniqueId` (L83), `hgAlphaPath` (L97, the brand α-Lissajous), `judgeBeats` (L122,
**derives the judge heartbeat from the drift** so the marks sit over the excursions).

### 4.4 The figures (one component per study renderer)

| Component | File:line | Ports study renderer | Props |
| --- | --- | --- | --- |
| `GanttZ` | `GanttZ.tsx:32` | `ganttSVG` | `{ z, selectedSpanId, onSpanSelect }` |
| `ChordZ` | `ChordZ.tsx:38` | `topoChordSVG` | `{ z, W=300 }` |
| `SeismographZ` | `SeismographZ.tsx:51` | `seismoSVG` | `{ z, W=940, axis }` |
| `JudgeHeartbeatZ` | `SeismographZ.tsx:272` | `heartSVG` | `{ z, W=940 }` |
| `LadderZ` | `LadderZ.tsx:25` | `ladderSVG` | `{ z, W=940 }` |
| `PlanZ` | `PlanZ.tsx:33` | `reelSVG` + `dagSVG` (reel drives DAG) | `{ z }` |
| `SequenceZ` | `SequenceZ.tsx:150` | `seqDiagramSVG` | `{ z, W=520, H=380 }` |
| `FingerprintZ` | `FingerprintZ.tsx:59` | `fpSVG` | `{ fp, status, id, size }` |
| `BrandMark` / `Wordmark` | `Brand.tsx:10/34` | brand block | — |

The two rail views compose them:
- `GanttViewZ.tsx:13` — `<GanttZ/>` (wired to `selectedSpanId`/`selectSpan` from the store) + `<JudgeHeartbeatZ/>`.
- `InstrumentsViewZ.tsx:25` — session head (`FingerprintZ`) → `<PlanZ/>` → panes-2 (`SequenceZ` | `ChordZ`) → the coordinated time-track stack (`SeismographZ` over `LadderZ`).

### 4.5 Theming

The zicato console has its **own** palette, independent of MD3:
- `zicatoTheme` in `uiStore` (16 study themes), applied via `data-zicato-theme` on `.zk-root`
  (`ZicatoConsole.tsx:520`) — it **never touches** the MD3 `<html data-theme>`, so there is
  no MD3 regression. Theme id list: `ZICATO_THEME_IDS` (`ZicatoConsole.tsx:41`).
- Tokens: `frontend/src/components/zicato/zicato-tokens.css` (715 lines — the per-theme role
  tokens `--paper/--panel/--ink/--good/--bad/--accent/--caution`, the `--ansi-*` ramp, and
  the categorical `--hg-kind-*` / `--hg-agent-*` / `--hg-gf-*` / `--hgraf-brand` tokens).
- Component CSS: `frontend/src/components/zicato/zicato.css` (1041 lines).
- Both are imported at the top of `ZicatoConsole.tsx` (L15–16).

---

## 5. File map

```
frontend/src/
  App.tsx                         ← uiMode branch (L48–51)
  state/uiStore.ts                ← uiMode + zicatoTheme (keys L83-84, type L367, init L536-548)
  components/shell/AppBar.tsx     ← ⧉ toggle to zicato (L73-81)
  components/zicato/
    ZicatoConsole.tsx   538  shell: topbar/strip/rail/main/inspector/transport/picker
    adapter.ts         1147  real session → ZSession (useZicatoSession L1012); ALL data mapping
    svgUtils.ts         140  shared render helpers (timeScale, judgeBeats, hgAlphaPath, …)
    GanttZ.tsx          393  hero gantt
    ChordZ.tsx          347  topology chord (thin gradient strokes, projection)
    SeismographZ.tsx    314  drift + judge seismograph (+ JudgeHeartbeatZ)
    LadderZ.tsx         128  intervention ladder (time-locked to the seismograph)
    PlanZ.tsx           413  plan reel → DAG
    SequenceZ.tsx       366  vertical sequence diagram
    FingerprintZ.tsx    119  session fingerprint identicon
    Brand.tsx            63  α mark + wordmark
    GanttViewZ.tsx       30  rail view: gantt
    InstrumentsViewZ.tsx 79  rail view: instruments
    zicato-tokens.css   715  per-theme token contract
    zicato.css         1041  component CSS
  __tests__/components/zicato.smoke.test.tsx   the scaffold gate (6 tests)
```

---

## 6. The design study (visual source of truth)

`docs/design/zicato-ui-study/` is the standalone, theme-switchable HTML/CSS/vanilla-JS
study the components port from. When a figure looks wrong, compare against the study —
the React components were written to match it 1:1. Key files:

- `compose.html` — the interactive composed console (the React shell + views mirror it).
- `grammar.html` — the figure catalogue (chord, seismograph, ladder, plan DAG, fingerprint, …).
- `sequence.html` — the sequence diagram + chord.
- `_tokens.css` / `_study.css` — the token contract the React `zicato-tokens.css`/`zicato.css` port.
- `STYLE-GUIDE.md`, `BRIEF.md`, `README.md`.

**Encoding rules (preserved in the port):** KIND = categorical/ANSI hue, STATUS = treatment
(running → accent + breathe, failed → `--bad` + ✕, pending → faint, planned → dashed,
awaiting → wait-for-human dash). `good`/`bad`/`accent` are earned by status, never identity.

**Recent study refinements** (also in this branch): arrowheads are **small solid triangles**;
chords are **thin gradient strokes** (width `min(4, 0.9+√count·1.1)`, non-scaling) rather than
filled teardrops; gantt transfer/delegation edges connect **span-end → span-start** with a
landing arrowhead; the judge heartbeat is **derived from the drift** so its marks line up
with the seismograph.

---

## 7. Side fix included on this branch — TrajectoryView header

While integrating, two **pre-existing** failures in
`frontend/src/__tests__/components/TrajectoryView.headerMath.test.tsx` were resolved (MD3
code, unrelated to zicato). Root cause: PR #195 added a `"Plan N · "` header prefix (for a
*merged-trajectory* model) + tests; PR #196 (newest) shipped the **plan picker**, which
scopes the view model to one plan and shows plan identity via chips — **superseding** the
prefix — but left #195's 2 prefix-tests stale. Fix: the header stays a plain `"rev N of M"`
(`planPrefix = ''` in `TrajectoryView.tsx`, ~L533), and the 2 stale tests were updated to
assert no-prefix while keeping their rev-math coverage. If the prefix is actually wanted,
that's the opposite call (un-scope the plan list for the header **and** flip
`TrajectoryView.planPicker.test.tsx`'s no-prefix assertion).

---

## 8. Known issues, risks & TODO (for whoever resumes)

1. **Verify against real data (blocking).** Nothing here has rendered a real session. Expect
   adapter bugs first: field mappings, time scale (`T`/`now`), edge derivation, plan
   reel→DAG, judge drift/beats. Start at `adapter.ts` builders (§4.3).
2. **Interactions unverified:** span → `ZicatoInspector`, chord hover/pin projection,
   reel→DAG drive, ⌘K session switch (`SessionPicker` is reused), theme switching across all
   16 zicato themes, and the toggle round-trip (only the button presence is asserted).
3. **No visual regression / a11y / cross-browser** pass yet.
4. **Bundle size:** `vite build` warns the single chunk is >500 kB (~785 kB raw / ~229 kB
   gzip). Pre-existing (heavy deps: MD3, `shiki`, protobuf). Advisory only. If load time
   matters, code-split: lazy-load `shiki` and the zicato console behind the toggle
   (`React.lazy` on the `App.tsx` branch).
5. **Not merged.** Lives on `design/sequence-compose-parity`.

---

## 9. Commands

```bash
# from frontend/
pnpm build      # tsc -b && vite build
pnpm lint       # eslint .
pnpm test       # vitest run (740 tests)
pnpm dev        # vite dev (set VITE_HARMONOGRAF_API to point at a backend)

# from repo root (Makefile)
make console        # build the SPA into the server static dir
make server-run     # run the server (serves gRPC-Web + the console)
make frontend-dev   # vite dev
make test           # all suites
```

Related memory/notes: `docs/design/zicato-ui-study/` (the study), and the project's design
intent. The toggle is intentionally default-off; keep it that way until §3/§8 are done.
