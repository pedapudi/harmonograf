# BRIEF — harmonograf UI study in the zicato design language

This is the **single source of truth** for the team building this study. Read it
fully, then read the source files it points to. Where this brief and a source
file disagree about a token/hex/class, **the source file wins** — re-grep.

## 0. Goal & deliverable

Produce a **UI study**: a self-contained set of HTML pages that show
harmonograf's console redrawn in **zicato's design language**, theme-switchable
across all 16 themes, with before/after comparisons per surface. This is a
*design artifact*, not production code — standalone HTML/CSS/JS, no build step,
opens from `file://`.

The native form of a study in this ecosystem is zicato's
`~/git/zicato/docs/design/tournament-viz-study/` — **clone its conventions
exactly** (self-contained pages, ported token block, `_embed.js` with
`?only/?bare/?theme`, a `compose.html`). Match its quality bar.

**Output dir:** `~/git/harmonograf/docs/design/zicato-ui-study/` (this dir).

## 1. The two design languages

**zicato** — canonical docs (read both, fully):
- `~/git/zicato/docs/design/DESIGN-LANGUAGE.md` — the *language* (tokens, type,
  line-art figure grammar §5, motion/digest-gating §7, a11y §9, and §10 a worked
  "build a harmonograf execution timeline" example — **use it**).
- `~/git/zicato/docs/design/CONSOLE-DESIGN-LANGUAGE.md` — the dashboard's
  *application* of it (figure catalogue §4.1, mark conventions §4.2).
- Authoritative token sheet: `~/git/zicato/src/zicato/dashboard/static/css/variants/T/console4.css`.
- A clonable study page (port its `--v2-*`→local token block + structure):
  `~/git/zicato/docs/design/tournament-viz-study/single-matchup.html`, and the
  shared `_embed.js` + `compose.html` in that dir.

Ethos in one line: **Tufte data-ink line-art, monospace-forward, a calm ground
with signal earned by direction, theme-adaptive by pure CSS swap, never
flashing.**

**harmonograf today** — what we are restyling (read the chrome + Gantt):
- `~/git/harmonograf/frontend/src/index.css` — ALL chrome classes (`.hg-*`):
  shell grid, appbar, rail, strip, transport, drawer, picker, help, panels.
- `~/git/harmonograf/frontend/src/theme/themes.ts` — MD3 tokens + the span-kind
  and goldfive-category hues we must re-home.
- `~/git/harmonograf/frontend/src/gantt/colors.ts` — kind×status → fill/stroke/
  treatment (the 2-axis encoding; preserve it, §3).
- `~/git/harmonograf/frontend/src/gantt/renderer.ts` — 3-layer canvas Gantt.
- Components: `frontend/src/components/{Gantt,OrchestrationTimeline,TaskStages,LiveActivity,Interventions,SessionPicker,TransportBar,shell}`.

It is **Material Design 3**: `--md-sys-color-*`, system-ui sans, elevation
containers, rounded radii, blue primary, several `animation:…infinite` pulses.

## 2. THE CENTRAL DECISION — full categorical palette from Gogh ANSI

zicato's "one green accent, everything else neutral" is built for a binary
promote/reject surface. harmonograf is a **categorical instrument**: many
agents, 9 span kinds, goldfive call categories. We are **keeping the full
categorical palette** — but sourcing every categorical hue from the active
theme's **terminal ANSI palette** (the Gogh scheme each zicato theme derives
from). This ties the categorical encoding to the theme system instead of
fighting it: switch theme → the whole palette re-skins, ANSI and all.

Preserve harmonograf's elegant **2-axis encoding**:
- **KIND = hue** → mapped to a theme ANSI color (full categorical, below).
- **STATUS = treatment** → mapped to zicato semantic roles + treatments
  (running → the *one* `--accent` + breathing; FAILED → `--bad` + error glyph;
  PENDING/CANCELLED → reduced opacity / hatch; planned → dashed). Status is the
  only place good/bad/accent appear, so zicato's "earned by direction" rule
  holds: a span is never red for its identity, only when it failed.

### 2.1 Span KIND → ANSI slot (proposed; foundation worker finalizes)

ANSI slots: `color_01..08` = black,red,green,yellow,blue,magenta,cyan,white;
`color_09..16` = bright variants.

| harmonograf kind | ANSI slot | rationale |
|---|---|---|
| `INVOCATION` | `color_09` bright-black/grey | structural container, recedes |
| `LLM_CALL` | `color_05` blue | |
| `TOOL_CALL` | `color_07` cyan | |
| `USER_MESSAGE` | `color_03` green | |
| `AGENT_MESSAGE` | `color_11` bright-green | distinct from user |
| `TRANSFER` | `color_04` yellow | |
| `WAIT_FOR_HUMAN` | `color_06` magenta | attention, but NOT red (red=failed) |
| `PLANNED` | `color_09` grey + dashed | ghost/planned |
| `CUSTOM` | `color_08` white/neutral | |

goldfive lane categories: judge severity → roles (on-task `--good`, warning
`--caution`, critical `--bad`, neutral `--flat`); `refine` → magenta `color_06`;
`plan` → cyan `color_07`; `reflective` → grey `color_09`.

### 2.2 Agent identity ramp

Agents (lanes/lifelines) get an **ordinal ramp** derived from tokens, not 8
saturated primaries — a low-sat sequence so lanes are distinguishable without
competing with kind hues or the accent. Build with `color-mix()` off ANSI
slots or an OKLCH spin; foundation worker decides and documents it.

## 3. Token / type mapping (MD3 → zicato roles)

| harmonograf (MD3) | zicato role / token |
|---|---|
| `surface` / `surface-container*` elevation ladder | `--paper` ground + ONE `--panel` lift; elevation→hairline `--rule`, not shadow |
| `primary` (#a8c8ff) | `--accent` — reserved for now/selected/live ONLY |
| `error` / `error-container` | `--bad` / `--bad-soft` |
| `tertiary` / transfer / wait hues | `--caution` |
| 9 `--hg-kind-*` | per-theme ANSI categorical (§2.1) |
| `outline` / `outline-variant` | `--rule` / `--rule-soft` |
| `on-surface` / `-variant` | `--ink` / `--ink-soft` / `--ink-faint` |
| system-ui body, 14px | `--mono`/prose-mono; data uses code-mono + `tabular-nums` |
| radii 4/8/12, pill chips | panels 4px, bars `rx:3`, `.dn-pill` mono outline-first |

Use the zicato `--v2-*` role values from `console4.css` /
`single-matchup.html` (paper/panel/ink/ink-soft/ink-faint/rule/rule-soft/
good/good-soft/bad/bad-soft/caution/accent/flat/cell-empty) for all 16 themes,
ported into local tokens exactly as the study pages do.

## 4. Surfaces to redesign (each gets before/after + rationale + legend)

1. **shell.html** — chrome: AppBar→zicato **topbar** (inline-SVG `zıcato`
   wordmark w/ dotless-ı + green dot, breadcrumbs, swatch theme picker, typeface
   switch, scale pill, status/RUN pill); NavRail; CurrentTaskStrip; TransportBar
   (transport controls, LIVE pill, clock); Drawer/inspector (tabs, attrs, code).
2. **gantt.html** — THE HERO. The execution Gantt in line-art: hairline
   gridlines (`--rule-soft`, 0.6w), `rx:3` bars at reduced fill-opacity lifting
   to 1 on hover, full categorical kind hues (§2), status treatments, the accent
   for the "now" cursor / selected span, agent gutter + lifelines, context band.
   Present BOTH options: (a) keep canvas, restyle via tokens; (b) fit-to-width
   SVG line-art. Use realistic mock multi-agent trace data.
3. **figures.html** — OrchestrationTimeline, TaskStages, LiveActivity (table as
   `.dn-board-table` idiom), Interventions, SessionPicker (⌘K). Pills mono
   outline-first; hovercards; tabular-nums.

## 5. File layout

```
zicato-ui-study/
  BRIEF.md            # this file
  STYLE-GUIDE.md      # foundation worker writes: final span→ANSI map, token map, do/dont
  _refs/gogh-palettes.json  # palette worker writes: 16 themes × full ANSI
  _tokens.css         # 16 themes: --v2 roles (from console4) + ANSI categorical (from json)
  _study.css          # shared page chrome + the .hg-* zicato-language component CSS
  _embed.js           # ported from zicato tournament-viz-study (?only/?bare/?theme)
  index.html          # overview + the categorical-palette thesis + links + theme picker
  shell.html
  gantt.html
  figures.html
  compose.html        # side-by-side, theme-synced, iframes of the above
```

Each page: ports/links the token block, ships the swatch theme picker
(default `monokai`), sets `<html data-theme="...">`, includes `_embed.js`.

## 6. Conventions to honor (from DESIGN-LANGUAGE.md)

- **Token-only color.** No hardcoded hex in any mark/component — read `--*`
  tokens so a theme swap is a pure re-skin. (The per-theme blocks in `_tokens.css`
  are the only place hex appears.)
- **Fit-to-width**, no pan/zoom; figures `width:100%` + `viewBox` + `role="img"`.
- **Motion discipline** (§7): the only keyframe is a status/now pulse, gated
  behind `@media (prefers-reduced-motion: reduce)`. No `animation:…infinite` on
  structure. Note digest-gating in the rationale (harmonograf already half-does
  it: "React never drives the render loop").
- **Focus rings**: `2px solid var(--accent)` `:focus-visible`, small offset.
- **Mono-forward** type; `tabular-nums` on all numerics/durations.
- **Verify in BOTH** a light theme (`paper`) and a dark one (`monokai`).

## 7. Gogh palette sourcing (palette worker)

Source: `https://raw.githubusercontent.com/Gogh-Co/Gogh/master/themes/<Name>.yml`.
Format (verified): `color_01..16` + `background` + `foreground`. color_01-08
normal black/red/green/yellow/blue/magenta/cyan/white; 09-16 bright.

Fetch these 16 (confirmed filenames in **bold**; others try canonical casing and
verify): **Belafonte Day**, **Belafonte Night**, **Dracula**, **Espresso**,
**Ubuntu**; Monokai, Solarized Dark, Solarized Light, Google Dark, Google Light,
Lunaria Light, Lunaria Eclipse, Paper, Zenburn, Selenized Black, Relaxed.

**Self-check:** each Gogh `background` MUST equal zicato's `--v2-paper` for that
theme (e.g. Dracula bg `#282A36` ✓, Belafonte Night `#20111B` ✓, Espresso
`#323232`, Ubuntu `#300A24`, Monokai `#1e1f1c`). zicato `--v2-paper` per theme is
in `DESIGN-LANGUAGE.md §2.2`. If a fetched bg doesn't match, you have the wrong
file — find the right one or note the discrepancy in the json. Emit
`_refs/gogh-palettes.json`: `{ "<theme-id>": { "ansi": {"black":..., "red":...,
... "brightWhite":...}, "bg":..., "fg":... }, ... }` keyed by zicato theme id.

## 8. Team structure (an agent team with an explicit LEAD)

- **Lead** owns the whole deliverable, coherence, and final QA. The Lead:
  reads this brief + sources; builds the foundation (`_tokens.css`,
  `_study.css`, `_embed.js`, `STYLE-GUIDE.md`) AFTER the palette worker returns
  (or builds it in parallel and wires the json last); spawns the three surface
  workers in parallel with crisp specs + the foundation contract; reviews each
  returned page for token-only color, fit-to-width, theme correctness, and
  cross-page visual coherence; assembles `index.html` + `compose.html`; runs the
  §6 validation in both `monokai` and `paper`; returns a summary of what was
  built, key decisions, and any open questions.
- **Palette worker** → §7, returns `_refs/gogh-palettes.json`.
- **Shell / Gantt / Figures workers** → §4, one page each, against the foundation.

Keep every page self-contained and theme-switchable. The bar is zicato's own
study pages — match it.
