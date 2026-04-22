# Example scenarios

Narrative walkthroughs of realistic harmonograf sessions. Each scenario
describes what the UI looks like at key moments, what log lines / drift
signals you should see, and what patterns are worth noticing. These are
**written walkthroughs, not runnable code** — they assume agents already
connected to your server.

If you want runnable code examples, see the test suites under
`client/tests/test_*.py` (especially `test_agent.py`,
`test_orchestration_modes.py`, and `test_drift_taxonomy.py`).

## Scenarios

1. [Single-agent research assistant with a tool error](research-tool-error.md) —
   one agent, a search tool that fails, a `TOOL_ERROR` drift firing a
   refine, and the replanned run.
2. [Multi-agent team with escalation to human review](escalation.md) —
   coordinator plus two specialists; a specialist escalates to a human
   decision via `WAIT_FOR_HUMAN`; operator approves from the drawer.
3. [Parallel map-reduce over a task plan](parallel-map-reduce.md) —
   parallel orchestration walking a DAG of independent tasks; how the
   Gantt visualizes fan-out with one row per ADK agent (#80).
4. [Delegated handoff with drift detection](delegated-handoff.md) —
   specialist agents hand off via AgentTool; drift detection fires on an
   unexpected transfer (now surfaces as `PLAN_DIVERGENCE` or
   `CONFABULATION_RISK` from the three-stage gate in goldfive#178).
5. [Long-running plan with cascading refines](cascading-refines.md) — a
   plan that replans several times over ~35 minutes on Qwen3.5-35B;
   how to read the cascade (including a 9-10 min STEER refine) without
   losing thread.

## How to read these

Every scenario follows the same shape:

- **Set-up** — who the agents are, what they are trying to do, how
  harmonograf is wired in.
- **Timeline** — a narration of the run moment by moment, with markers
  like **t=0** at the start of each interesting transition.
- **What the UI looks like** — short descriptions of the region layouts
  at each moment. Screenshots are marked `TODO:` for replacement.
- **Log lines / attributes** — the span attributes and server-side log
  lines that accompany each moment. These are literal enough that you
  can grep for them in your own runs.
- **Patterns to notice** — a short list of the reusable observations.

## Related

- [cookbook.md](../cookbook.md) — the recipes that the scenarios exercise.
- [faq.md](../faq.md) — quick answers to adjacent questions.
- [glossary.md](../glossary.md) — term definitions.
- [../tasks-and-plans.md](../tasks-and-plans.md) — the drift kinds table referenced throughout.
