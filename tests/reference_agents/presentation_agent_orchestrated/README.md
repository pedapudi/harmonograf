# presentation_agent_orchestrated

Orchestration-mode sibling of `tests/reference_agents/presentation_agent/`.
Both packages share the same coordinator + research + web_developer +
reviewer + debugger tree (loaded by absolute file path from
`third_party/goldfive/examples/presentation_agent/agent.py`); the only
intentional difference is that this module wraps the tree with
`goldfive.wrap(...)` before handing it to `App()`.

## What orchestration mode adds

| Layer | Observation (`presentation_agent`) | Orchestration (`presentation_agent_orchestrated`) |
|---|---|---|
| Routing | Coordinator LLM picks the next sub-agent via its instruction text. | Goldfive derives a goal, plans the specialists, and dispatches them in order. |
| Plan | None — the LLM's transcript *is* the plan. | Explicit `TaskPlan` with `research → build → review → debug`. |
| Drift | Not detected; the coordinator just keeps generating. | `TaskCompleted` fires per sub-agent; adapter-return mismatches trigger plan revision. |
| Steering | Transcript-level only. | Goldfive's steering + HITL seams surface into harmonograf, so PAUSE / STEER / CANCEL take effect mid-run. |

If you want to see the full goldfive drift / refine / user-actor
behaviour end-to-end in `adk web`, load this package. Observation mode
only emits per-span telemetry — the interesting orchestration events
(plan submission, task dispatch, drift) never fire because nothing is
doing the orchestrating.

## How `adk web` picks between the two

`make demo` / `make demo-presentation` stage **both** packages under
`.demo-agents/` and point `adk web` at that directory. ADK's picker
then lists both by name:

* `presentation_agent` — observation
* `presentation_agent_orchestrated` — orchestration (this module)

Pick whichever you want to drive from the ADK UI; the harmonograf tab
receives the appropriate telemetry regardless of which one is running.

## Env vars

* `OPENAI_API_KEY` — present → live mode (`openai` SDK behind
  `LLMPlanner` / `LLMGoalDeriver`). Absent → mock mode so the `App`
  builds offline with a canned plan.
* `USER_MODEL_NAME` — ADK sub-agent model string (default
  `gemini-2.5-flash`).
* `GOLDFIVE_EXAMPLE_TOPIC` — default topic for the mock planner
  (default `waffles`).
* `GOLDFIVE_EXAMPLE_PLANNER_MODEL` — OpenAI model for the planner /
  goal deriver in live mode (default `gpt-4o-mini`).
* `HARMONOGRAF_SERVER` — `host:port` for the harmonograf server. When
  `harmonograf_client` is importable a telemetry plugin is attached.
