# Scenario: long-running plan with multiple refines and drift cascades

A plan (twelve tasks) running over ~35 minutes on a local Qwen3.5-35B.
Four drift events fire, each producing a plan revision after the
planner's refine LLM returns. This scenario is about **reading the
cascade** without losing the thread of what happened.

Four drifts hit one long session, each producing a plan revision. Use this as a map for the timeline below.

```mermaid
timeline
    title Cascading refines over ~35 minutes (Qwen3.5-35B)
    t=0 : session picked : 12 tasks pending
    t=4m : AGENT_REFUSAL (red) : +1 ~1 reframe
    t=8m : TOOL_ERROR (red) : +2 retry tasks
    t=16m : PLAN_DIVERGENCE (amber) : cross-layer tool call
    t=22m-32m : USER_STEER (purple) : operator redirect · planner refine takes 9-10 min
    t=35m : COMPLETED

## Set-up

- Agents: `writer-coordinator` (`SEQ`), `drafter`, `fact-checker`,
  `editor`.
- Initial plan: twelve tasks — outline, draft N sections, fact-check,
  edit, finalize. All assigned across the three specialists.
- Session runs for ~35 minutes. Operator is occasionally in the room.

## Timeline

### t=0 — session picked

Normal `SEQ` start. Current task strip reads
`Currently: Outline · RUNNING · SEQ · writer-coordinator`. Task panel
shows twelve rows, one RUNNING.

### t=4 min — first drift: `AGENT_REFUSAL`

The drafter's LLM refuses the request outright (safety filter). The
goldfive drift detector catches the refusal, emits a
`DriftDetected(kind=AGENT_REFUSAL, severity=critical)` event, and the
planner refines.

Intervention timeline strip marker: red critical ring around a chevron
glyph. Popover:

- Source: `drift`, kind `AGENT_REFUSAL`.
- Body: `drafter refused: safety filter`.
- Outcome: `→ rev 1` once the planner returns (typically 30-60 s on
  Qwen3.5-35B).
- Plan diff: `+1 ~1`. New `Reframe section 3` task plus a modified
  `Draft section 3` description.

### t=8 min — second drift: `TOOL_ERROR`

The fact-checker calls a lookup tool that times out. Goldfive sees the
tool-error span on its `after_tool_callback` and emits
`DriftDetected(kind=TOOL_ERROR, severity=warning)`.

Strip marker: amber dashed-ring circle.

- Body: `lookup(dates) failed: timeout`.
- Outcome: `→ rev 2`. The planner adds two retry tasks with different
  tool arguments.

### t=16 min — third drift: `PLAN_DIVERGENCE`

The drafter tries to call the fact-checker's tool directly (wrong
layer of the plan — the three-stage gate from goldfive#178). Goldfive
fires `DriftDetected(kind=PLAN_DIVERGENCE, severity=warning)` and
refines.

Strip marker: amber chevron.

- Body: `drafter called cross-layer tool check_source`.
- Outcome: `→ rev 3`.

### t=20 min — drafter goes quiet

No new bars on `drafter`'s row for several minutes. You switch to the
Graph view. The `drafter` header has gone amber with a ⚠ stuck label
(the liveness tracker flagged an open INVOCATION with no recent
progress). See [graph-view.md → agent headers](../graph-view.md#agent-headers).

Click `↻ Status` on the drafter header. The button spins, then within
8 seconds the task report line updates to `Waiting on fact-checker for
citation list`. Not actually stuck — waiting on a cross-agent
dependency that harmonograf can't see from span structure.

You might annotate the drafter span with a `COMMENT`:
`"waiting on cross-agent, not hung"` (drawer → Annotations tab,
[annotations.md → drawer annotations tab](../annotations.md#drawer--annotations-tab)).

### t=22 min — fourth drift: `USER_STEER` (the slow one)

You decide the draft is going too long and push a steer. Hover the
drafter's active INVOCATION on the Gantt, click **Steer** in the
popover, type `"Cut section 4 to two paragraphs."`, `⌘↵`.

The UI acks in ~1 second:

- The annotation lands on the strip as a purple diamond marker.
- The agent's next LLM_CALL picks up the STEER body on its next turn.

Then the slow path:

- The planner observes the drift and calls its refine LLM with the
  full task state + steer body.
- On Qwen3.5-35B this refine routinely takes **9-10 minutes** on a
  long-context task list. The intervention card's outcome chip stays
  at `pending` the entire time.
- Finally the planner returns a revised plan. The strip marker's
  outcome chip flips to `→ rev 4`, and the drift + plan_revised
  events fold into the annotation card via the 5-minute attribution
  window (harmonograf#86 / the extended `_USER_OUTCOME_WINDOW_S`).

Plan diff: `+0 ~1`. Section 4 task gets its description trimmed.

### t=34 min — plan completes

The editor's final INVOCATION closes. All twelve-plus-inserted tasks
are `COMPLETED`. Current task strip reads `Currently: Finalize ·
COMPLETED · SEQ · editor`. Four plan revisions in the history.

## How to read the cascade

Four drifts in a 35-minute run. The banner is transient (pills dismiss
in ~4 s) and dedupes on identical `revisionReason` strings, so it is
lossy by design. The **drawer → Task → Plan revisions** section is
the durable record; open it on any task-bound span of this plan and
scroll.

Reading order:

1. **Sort by timestamp.** Revisions list newest-first. The latest is
   expanded by default; click older rows to expand.
2. **Eyeball the category strip** across every revision. Red
   (`llm_refused`), green (`new_work_discovered`), grey-blue
   (`context_pressure`), blue (`user_steer`). Four different
   categories = cascade with heterogeneous causes.
3. **Cross-reference the orchestration events** at the bottom of the
   Task tab. Filter by kind to see only `divergence` events, for
   example — see [drawer.md → orchestration events section](../drawer.md#orchestration-events-section).
4. **For each revision, check `drift_severity`.** In this cascade:
   `llm_refused` is critical, `context_pressure` is warning,
   `new_work_discovered` and `user_steer` are info. You can ignore
   info-severity drifts during triage.
5. **Use `COMMENT` annotations to mark your own observations** so your
   future self has context. They persist on reload and show as pins
   over the Gantt.

## Log lines / attributes

Four `DriftDetected` events land in goldfive's event stream:
`AGENT_REFUSAL`, `TOOL_ERROR`, `PLAN_DIVERGENCE`, `USER_STEER`. Each
`TaskPlan` revision carries the matching `revision_kind` and
`revision_severity`, and the intervention aggregator surfaces them as
four cards on the strip.

The slow STEER can be spotted by reading `goldfive.llm.duration_ms`
off the planner's LLM_CALL span that followed the STEER — expect
300000+ (5+ minutes) on Qwen3.5-35B for a heavy plan refine.

If the STEER card's outcome chip is stuck at `pending` for more than
a few minutes, the planner may be wedged — see
[runbooks/high-latency-callbacks.md](../../../runbooks/high-latency-callbacks.md).

## Patterns to notice

1. **Long sessions always have banner gaps.** Never trust the pill
   stack as a record; always read the drawer's Plan revisions section.
2. **Drift kinds come in waves.** `context_pressure` often follows
   a `new_work_discovered` that added length. Treat the cascade as
   causally linked, not independent.
3. **Amber ≠ dead.** Always `STATUS_QUERY` before cancelling a
   stuck-looking agent; waiting on another agent is a legitimate idle
   state that looks identical to "process hung" from the Gantt alone.
4. **Steer control vs. steer annotation matters in long sessions.**
   One-shot controls (fast) leave no durable record beyond the drift
   revision; if you want to leave a persistent "this is why I
   intervened" note, post a `STEERING` annotation alongside the
   control. See [annotations.md → steering from the popover](../annotations.md#steering-from-the-popover-is-a-control-not-an-annotation).
5. **Session-level triage lives in the drawer's Task tab.** Don't try
   to eyeball cascades from the Gantt alone — it is span-oriented,
   not plan-oriented. The task panel + Task tab are the plan-oriented
   surfaces.

## Related

- [cookbook.md → compare two plan revisions](../cookbook.md#4-compare-two-plan-revisions)
- [cookbook.md → filter and triage by drift kind](../cookbook.md#7-filter-and-triage-by-drift-kind)
- [cookbook.md → confirm an agent is still alive](../cookbook.md#10-confirm-an-agent-is-still-alive-when-the-ui-looks-frozen)
- [tasks-and-plans.md → drift kinds](../tasks-and-plans.md#drift-kinds)
- [faq.md → how do I know the context window is full](../faq.md#how-do-i-know-the-context-window-is-full)
