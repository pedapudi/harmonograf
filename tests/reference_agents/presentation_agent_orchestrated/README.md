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
| Plan | None — the LLM's transcript *is* the plan. | Explicit `Plan` with `research → build → review → debug`; revisions stored with per-rev diffs. |
| Drift | Not detected; the coordinator just keeps generating. | `TaskCompleted` fires per sub-agent; adapter-return mismatches trigger plan revision; all drift events ride `HarmonografSink` to the server. |
| Steering | Transcript-level only. | Goldfive's steering + HITL seams surface into harmonograf via `ControlBridge`, so PAUSE / STEER / CANCEL from the UI take effect mid-run (harmonograf#72 validates STEER body; server stamps author / annotation_id). |
| Trajectory | Empty. | Every drift / plan-rev / user-control event shows up as an intervention marker (harmonograf#69 / #71 / #76); user-control kinds merge inside a 5 min window (#81 / #87). |
| Per-agent rows | Plugin attribution still works — one row per ADK agent (harmonograf#74 / #80). | Same — plugin is installed on the wrapped Runner's ADK adapter. |

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

Under lazy Hello (harmonograf#85), the client defers its Hello until
the first real emit, so there's no ghost session row for whichever
variant you didn't pick. Session unification (#66) pins the outer
adk-web session id on `goldfive.Session.id`, so the row you see in the
picker matches the ADK session you're driving.

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
