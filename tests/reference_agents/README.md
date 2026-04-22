# Reference agents

This directory holds reference ADK agents used for demos (`make demo`,
`make demo-presentation`) and end-to-end tests under `tests/e2e/`. They
are illustrative samples — not production code — and exist to exercise
the `harmonograf_client` instrumentation against a real ADK runner.

Two sibling packages:

| Package | Mode | What it proves |
|---|---|---|
| [`presentation_agent/`](presentation_agent/) | Observation | `HarmonografTelemetryPlugin` attached to a plain ADK `App`. No `goldfive.wrap`. Plans/tasks/drift stay empty; per-span telemetry, per-ADK-agent Gantt rows, and control still work. |
| [`presentation_agent_orchestrated/`](presentation_agent_orchestrated/) | Orchestration | Same agent tree wrapped with `goldfive.wrap(...)` before `App()` sees it. Goldfive derives a goal, plans specialists, dispatches, fires drift; harmonograf shows plan/task/drift/intervention surface end-to-end. |

Both packages share the same coordinator + research + web_developer +
reviewer + debugger tree, loaded by absolute file path from
`third_party/goldfive/examples/presentation_agent/agent.py` so the two
stay byte-identical.

`make demo` stages both under `.demo-agents/` and `adk web` lists them
both in the picker — pick whichever mode you want to drive from the UI.

Keep dependencies minimal: a reference agent should pull in ADK,
`harmonograf_client`, and (for the orchestrated sibling) `goldfive`;
nothing else from this repo. Do not import from `client/` internals or
from `server/` runtime code; if a reference agent needs a helper, the
helper belongs in the public client API first.
