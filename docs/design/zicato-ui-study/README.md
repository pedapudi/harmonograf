# harmonograf UI study — in the zicato design language

A self-contained, theme-switchable study showing harmonograf's MD3 console
redrawn in **zicato's design language**, keeping harmonograf's full categorical
palette but sourcing every categorical hue from each theme's **Gogh terminal
ANSI palette**. Standalone HTML/CSS/vanilla-JS — no build step.

## Open it

Everything opens straight from `file://` — no build step, no server. Start at
**`index.html`** (the thesis, surface links, 16-theme wall), or jump to
**`compose.html`** for the full interactive console.

## Files

```
index.html       overview + the categorical-palette thesis + links + theme picker
shell.html       chrome before/after: topbar · rail · strip · transport · drawer
gantt.html       THE HERO — the execution timeline (SVG line-art + canvas options)
sequence.html    how the agents talk: the vertical sequence diagram (recommended)
                 + the directional transfer chord (topology) + the score (alt)
figures.html     OrchestrationTimeline · TaskStages · LiveActivity · Interventions · ⌘K
grammar.html     fourteen new figures: seismograph · ladder · sankey · strata ·
                 plan DAG · delegation score · chord · fingerprint · …
brand.html       harmonograf's own mark — the Lissajous α, wordmark, lockups
compose.html     THE COMPOSED CONSOLE — interactive. The gantt as its own hero
                 view + one coalesced "instruments" view leading with the plan:
                 the plan REEL sits above the plan DAG and DRIVES it — click a
                 revision and the DAG redraws that version's task set (added in
                 accent, dropped-since-prior ghosted/dashed). Then sequence ·
                 projectable topology chord · and ONE coordinated time-track
                 stack: the drift seismograph with the judge heartbeat folded in
                 (one time axis) over the time-aligned intervention ladder.
                 Sessions ONLY via the ⌘K fingerprint picker (no sidebar).
                 Clickable spans → inspector. ?ver= deep-links a reel revision.
                 (The earlier A/B/C whole-UI proposals + fleet sidebar live in
                 git history.)

_tokens.css      16 themes: zicato --v2 roles + --ansi-* + the --hg-* categorical
_study.css       shared page chrome + the .hg-* zicato-language component CSS
_embed.js        ?only / ?bare / ?theme (ported from zicato tournament-viz-study)
_refs/gogh-palettes.json   16 themes × full ANSI, self-checked vs --v2-paper

STYLE-GUIDE.md   the foundation contract: span-KIND→ANSI map, agent ramp,
                 MD3→zicato token map, the accent-collision rule, do/don'ts,
                 and the open questions
BRIEF.md         the original brief (single source of truth)
```

## The one idea

zicato spends **one** green accent on a calm ground. harmonograf is a
**categorical** instrument, so this study keeps the full palette but sources
every hue from the active theme's **terminal ANSI** set. The 2-axis encoding
holds: **KIND = hue** (an ANSI slot), **STATUS = treatment** (running → the one
`--accent` + breathe; failed → `--bad` + `✕`; pending → faint; cancelled → hatch;
planned → dashed). good / bad / accent appear **only** for status — a span is
never red for its identity, only when it failed. Switch theme → the whole thing
re-skins, ANSI and all, with no re-render. See `STYLE-GUIDE.md`.
