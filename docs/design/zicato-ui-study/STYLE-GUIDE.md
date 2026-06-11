# STYLE-GUIDE — harmonograf in the zicato design language

The foundation contract. Surface pages (`shell.html`, `gantt.html`,
`figures.html`) build against this. It finalizes the proposals in
[BRIEF.md](BRIEF.md): the span-KIND→ANSI map, the agent ramp, the MD3→zicato
token map, and the do/don'ts. Where this and a source file disagree about a
token, **the source wins** — re-grep.

Files in this study:

```
_refs/gogh-palettes.json   16 themes × full ANSI, self-checked vs --v2-paper
_tokens.css                16 theme blocks: --v2 roles + --ansi-* + --hg-* categorical
_study.css                 shared page chrome + the .hg-* zicato-language components
_embed.js                  ?only / ?bare / ?theme (ported from zicato study)
STYLE-GUIDE.md             this file
index.html  shell.html  gantt.html  figures.html  compose.html
```

---

## 1. The thesis (why this study exists)

zicato is a **binary** promote/reject surface: one green accent, everything else
neutral. harmonograf is a **categorical instrument**: many agents, 9 span kinds,
goldfive call categories. We keep harmonograf's full categorical palette — but
**source every categorical hue from the active theme's terminal ANSI palette**
(the Gogh scheme each zicato theme derives from). Switch theme → the whole
categorical palette re-skins, ANSI and all. This ties the encoding to the theme
system instead of fighting it.

The elegant **2-axis encoding** is preserved:

- **KIND = hue** → a theme ANSI slot (`--hg-kind-*`, full categorical).
- **STATUS = treatment** → zicato semantic roles + treatments. Status is the
  **only** place good / bad / accent appear, so zicato's "earned by direction"
  rule holds: a span is never red for its identity, only when it failed.

---

## 2. Span KIND → ANSI slot (FINAL)

ANSI slots map onto `--ansi-*` tokens in `_tokens.css`; the kind tokens alias them.

| harmonograf kind | token | ANSI slot | rationale |
|---|---|---|---|
| `INVOCATION` | `--hg-kind-invocation` | bright-black (grey) | structural container, recedes (rendered at low opacity) |
| `LLM_CALL` | `--hg-kind-llm-call` | blue | |
| `TOOL_CALL` | `--hg-kind-tool-call` | cyan | |
| `USER_MESSAGE` | `--hg-kind-user-message` | green | |
| `AGENT_MESSAGE` | `--hg-kind-agent-message` | bright-green | distinct from user |
| `TRANSFER` | `--hg-kind-transfer` | yellow | |
| `WAIT_FOR_HUMAN` | `--hg-kind-wait-for-human` | magenta | attention, but **NOT red** (red = failed) |
| `PLANNED` | `--hg-kind-planned` | bright-black (grey) | ghost / planned + **dashed** treatment |
| `CUSTOM` | `--hg-kind-custom` | white / neutral | |

**goldfive** lane categories (`--hg-gf-*`): judge severity → roles
(`on-task`→`--good`, `warning`→`--caution`, `critical`→`--bad`,
`neutral`→`--flat`); `refine`→magenta; `plan`→cyan; `reflective`→bright-black.

### 2.1 STATUS treatments (the second axis)

Applied **over** the kind hue. The renderer/marks read these; never bake a status
color into a kind.

| status | treatment |
|---|---|
| `COMPLETED` | kind hue at fill-opacity `0.55` idle → `1.0` on hover |
| `RUNNING` | **`--accent`** stroke (w 2) + the breathing pulse (`hg-breathe`, reduced-motion → static) |
| `FAILED` | fill `--bad` + the `✕` error glyph (1:1 overlay) |
| `PENDING` | kind hue at opacity `0.35`, no stroke |
| `CANCELLED` | opacity `0.30` + diagonal hatch |
| `AWAITING_HUMAN` | fill `--bad-soft`, stroke `--bad`, the `⏱`/`◷` attention glyph; pulses |
| `PLANNED` (kind) | `--rule-soft` fill + `--ink-faint` **dashed** outline |
| `replaced` | opacity `0.30` (stacks with status) |

---

## 3. The accent-collision rule (the categorical-vs-one-accent tension)

The hard part. Because zicato derived each theme's `--v2-accent` **from** its
ANSI cyan/blue, in several themes a kind hue's base hex **equals** the accent:

| collision | themes |
|---|---|
| `LLM_CALL` (blue) == accent | monokai, solarized-light, belafonte-day, paper, espresso |
| `TOOL_CALL` (cyan) == accent | solarized-dark, google-dark, lunaria-light, relaxed |

Resolution — **distinguish the accent by TREATMENT, never rely on hue:**

1. **Kind hues are always quieted fills** (idle fill-opacity `0.5–0.6`, lifting to
   `1` only on hover). The accent appears only as a **full-strength stroke** (the
   now-cursor, the selected-span spine, a focus ring) + the breathing pulse — a
   treatment **no kind fill ever takes**. So even when `LLM_CALL` and `--accent`
   share a base hex, the live/selected mark is unmistakable: it is the stroked,
   breathing one.
2. **`--accent` is reserved** for now / selected / live / interactive focus —
   full stop. It is never spent on a kind, a legend swatch's identity, or chrome
   decoration.
3. Legends label kind by **name + a quieted swatch**, so a blue==accent theme
   still reads "LLM_CALL" unambiguously from the label, not the chip alone.

This is the honest version of "keep the categorical palette": the categorical
layer and the emphasis layer are separated by **role and treatment**, not by
holding 9 hues that are all guaranteed distinct from the accent in all 16 themes
(impossible given the derivation). Documented as an open question for Sunil (§8).

---

## 4. Agent identity ramp (FINAL)

harmonograf today hashes `agent_id` onto **Tableau-10** (8 saturated primaries) —
exactly the "8 saturated primaries" the brief says to replace. Instead, lanes get
an **ordinal ramp** derived from tokens:

```
--hg-agent-1..8 : color-mix(in oklab, <ANSI slot> 62%, var(--v2-ink-faint))
```

slots, in order: `blue, cyan, green, yellow, magenta, bright-blue, bright-green,
bright-yellow`. The 62% mix toward `--ink-faint` **desaturates** every lane so
the gutter/lifelines stay distinguishable without competing with the (full-sat)
kind hues on the bars or with the accent. Two synthetic actors are fixed:

- `--hg-agent-user` — caution-mixed (warm neutral, reads as "person").
- `--hg-agent-goldfive` — accent-mixed (cool neutral, reads as "orchestrator").

Lifelines/gutter labels use the agent color at low strength; the bars on a lane
take the **kind** hue, not the agent hue (agent identity is the lane, kind is the
bar). All theme-adaptive: a swap re-mixes the ramp.

---

## 5. MD3 → zicato token map (FINAL)

| harmonograf (MD3) | zicato role / token |
|---|---|
| `surface` / `surface-container*` elevation ladder | `--paper` ground + ONE `--panel` lift; elevation → hairline `--rule`, **not** shadow |
| `primary` (#a8c8ff) | `--accent` — reserved (now / selected / live / focus) |
| `primary-container` | `color-mix(--accent 14%, --panel)` for selected rows |
| `error` / `error-container` | `--bad` / `--bad-soft` |
| `tertiary` / transfer / wait hues | `--caution` (chrome) ; kind hues use ANSI (§2) |
| `secondary-container` (rail selected) | `color-mix(--accent 14%, --panel)` + accent text |
| 9 `--hg-kind-*` | per-theme ANSI categorical (§2) |
| `outline` / `outline-variant` | `--rule` / `--rule-soft` |
| `on-surface` / `-variant` | `--ink` / `--ink-soft` / `--ink-faint` |
| system-ui body, 14px | `--mono` everywhere; data uses mono + `tabular-nums` |
| radii 4 / 8 / 12, pill chips | panels **4px**, bars `rx:3`, `.dn-pill` mono outline-first 10px |

### 5.1 Type

All-mono, terminal-forward (the study's default face stack is
`ui-monospace, "JetBrains Mono", …`). `tabular-nums` on every numeric / duration /
clock. Headings = same mono, slightly heavier; section eyebrows uppercase with
`0.12em` tracking. No system-ui sans anywhere.

### 5.2 The dotless-ı wordmark

The topbar carries the inline-SVG `zıcato` wordmark (dotless ı U+0131, the green
accent circle **is** the i's dot) + the spiral brand mark, both `currentColor`
strokes with `var(--zicato-accent)` dots — ported from the zicato compose.html
(`MARK_SPIRAL` / the wordmark builder). A `.dt-brand-variant` tag reads
`console`.

---

## 6. Line-art conventions (from DESIGN-LANGUAGE.md §5)

- Hairline gridlines: `stroke:var(--rule-soft); stroke-width:0.6; vector-effect:
  non-scaling-stroke`. No gridframe, no 3-D, no chartjunk.
- Bars: `rx:3`, fill the **kind** token at reduced `fill-opacity` (`0.55`),
  lifting to `1` on hover. `stroke:none` except status treatments.
- The one emphasis: `--accent`, stroke-width 2–2.4 (now-cursor / selected spine).
- Reference / planned: `--ink-faint` dashed (`stroke-dasharray:3 3` / `4 3`).
- Every figure **fit-to-width**: `width:100%` + `viewBox` + `role="img"`; a glyph
  that must stay round goes in a separate 1:1 overlay. No pan/zoom, no fixed px
  width that overflows.
- Hovercards: a singleton card on `--panel` / `--rule`, mono, `pointer-events:
  none`, outside any gated render.

## 7. Motion & a11y

- The **only** keyframe is a now/running pulse (`hg-breathe`, `hg-now-pulse`),
  gated behind `@media (prefers-reduced-motion: reduce)` → static. **No**
  `animation:…infinite` on structure (kills harmonograf's MD3 `*-pulse` loops).
- Note in rationale: harmonograf already half-does digest-gating ("React never
  drives the render loop" — `renderer.ts`); the study makes that explicit.
- Focus rings: `2px solid var(--accent)`, small offset, `:focus-visible`.
- Verify every page in BOTH `paper` (light) and `monokai` (dark).

## 8. Do / Don't (study-specific)

**Do** — read color from `--*` tokens only; earn good/bad by direction; reserve
`--accent`; quiet kind fills, full-strength only on hover/selection; mono + tabular
everywhere; one `--panel` lift, hairlines not shadows; gate motion.

**Don't** — hardcode hex in a mark (the per-theme `_tokens.css` blocks are the
only hex); spend `--accent` on a kind or chrome decoration; color a span red for
its identity (only FAILED is red); use system-ui sans; run structure on
`animation:…infinite`; force horizontal scroll; pin a figure to a fixed px width.

### 8.1 Open questions for Sunil

1. **Accent collisions (§3).** Resolved by treatment, not hue. If you'd rather the
   kind hues be guaranteed-distinct from the accent in every theme, we'd nudge
   `LLM_CALL`/`TOOL_CALL` off the accent slot per-theme (a per-theme override
   table) — more faithful-to-ANSI vs. more separable. Current choice favors ANSI
   fidelity + treatment-based distinction.
2. **Monokai ANSI source.** zicato's `monokai` is "original" lineage, not Gogh;
   its closest Gogh file (Monokai Soda, bg `#1A1A1A`) doesn't match `--v2-paper`
   `#1e1f1c` and uses different greens. We sourced monokai's ANSI from the
   **classic** Monokai terminal palette (matches zicato's good/bad/accent roles).
   Flagged in `gogh-palettes.json`.
3. **Zenburn green.** Zenburn's ANSI "green" slot is actually a yellow; we used
   its sage (`#7f9f7f`) so USER_MESSAGE stays green-ish. Muted by design.
4. **belafonte-night hue-collapse.** On this ONE theme the ANSI kind slots
   collapse into near-identical warm greys (blue `#426A79` / cyan `#989A9C` /
   green `#858162` / white `#968C83` / bright-black `#5E5252`), so the categorical
   *hues* blur there — kinds stay legible by **treatment + named legend/hovercard**
   (FAILED red, RUNNING accent-stroke, TRANSFER yellow, dashed/hatch, gutter
   ticks), not by hue. Same "more separable vs more ANSI-faithful" tradeoff as
   §8.1.1: a remedy would spin belafonte-night's collapsed kinds apart in
   `_tokens.css`. Left ANSI-faithful by default; flagged by the Gantt + Figures
   review pass. (The collision-theme recheck — espresso / solarized-light /
   google-dark / dracula — otherwise PASSED, validating the treatment-based §3
   resolution.)
