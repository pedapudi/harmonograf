# Harmonograf milestones

Incremental plan for continuing Harmonograf implementation. Each milestone
ends with a concrete thing the user can run locally and give feedback on.
Inside each milestone, 4–5 streams run in parallel.

Auth is deferred to its own project — local-only for now.

## Milestone A — "Drive presentation_agent from adk, see it live in harmonograf"

End state: `make demo` boots harmonograf-server + frontend + `adk web
presentation_agent` together. User drives a presentation from adk's own
UI and watches the timeline populate in harmonograf's UI in real time.

- **A1** — finish golden-path Playwright smoke (task #6) + sonora Origin
  echo (task #8) so the browser smoke passes green end-to-end.
- **A3** — session picker lists real sessions from `ListSessions` and
  auto-selects the newest on load.
- **A4** — integrate `attach_adk` **inside** `tests/reference_agents/presentation_agent/agent.py`
  where `root_agent` is constructed, so every invocation driven through
  adk's own runner automatically reports to harmonograf. Remove or
  repurpose `run_harmonograf.py`. Update Makefile `demo-presentation`
  target to invoke the canonical adk CLI.
- **A5** — ADK adapter: emit `TRANSFER` spans for `AgentTool` sub-dispatch
  (task #7) so transfers read as transfers in the Gantt rather than as
  tool calls.
- **A6** — one-command `make demo` that boots harmonograf-server, Vite
  frontend, and `adk web presentation_agent` together and prints both
  URLs.

**User test at end of A:** run `make demo`, open adk's UI in one tab and
harmonograf in another, drive a presentation through adk, watch the
timeline populate in real time. Give feedback on what feels wrong.

## Milestone B — "Richer multi-agent presentation_agent"

End state: coordinator drives five sub-agents in a meaningful flow; the
timeline tells a clear story with visible cross-agent linkages.

- **B1** — expand `presentation_agent` with **reviewer_agent** and
  **debugger_agent** as additional sub-agents of the coordinator.
  - `reviewer_agent`: reviews `web_developer_agent`'s generated
    HTML/CSS/JS and produces a critique.
  - `debugger_agent`: invoked if `write_webpage` fails or the reviewer
    flags issues; edits the generated files in place.
  - Coordinator flow: research → web_developer → reviewer →
    (optional) debugger → report.
- **B2** — cross-agent span links in the UI (`LINK_INVOKED` edges,
  visible on the canvas layer).
- **B3** — live-tail toggle (follow-the-head cursor), important once
  invocations run longer and span multiple agents.
- **B4** — agent gutter sorting + show/hide row filter.
- **B5** — span detail polish — arguments, return values, errors all
  legible in the Inspector drawer.

**User test at end of B:** run the expanded demo, watch five agents
coordinate across a single presentation, verify the timeline tells a
clear story.

## Milestone C — "Interact with running agents"

End state: clicking in the UI steers a running agent or resolves a
blocking HITL prompt.

- **C1** — wire Inspector Drawer "Control" tab to `SendControl` →
  ControlRouter → client `SubscribeControl` → ADK adapter actually
  pauses / cancels / injects.
- **C2** — HITL approval editor: resolve a blocked long-running tool via
  `ApprovalEditor` → tool returns value → invocation continues.
- **C3** — annotations round-trip (create, edit, delete) with optimistic
  UI and server persistence.
- **C4** — demo script or scenario that deliberately triggers a HITL
  prompt so the user can exercise C2.

**User test at end of C:** pause / cancel a running agent from the UI;
approve a HITL prompt from the UI and watch the agent continue.

## Milestone D — "Durable + observable single-node"

End state: server restart preserves sessions; operators have visibility
into load and retention behavior.

- **D1** — wire `SqliteStore` into the CLI + `make demo` target so
  durability is the default demo path.
- **D2** — `make stress` produces a report; stats page surfaces live
  `GetStats` data in the UI.
- **D3** — retention / GC flags verified with a small integration test
  and a doc snippet.
- **D4** — structured log sink plus a sample journald/Loki integration
  doc.

**User test at end of D:** run stress, `kill -9` the server, restart,
verify sessions are still present and queryable.

## Milestone E — "Polish + package"

End state: repo is in a state you could send to a colleague.

- **E1** — error states (disconnected client, failed span, empty
  session) rendered in the UI.
- **E2** — README top-level demo GIF.
- **E3** — dark / AMOLED theme pass based on feedback from earlier
  milestones.
- **E4** — CI runs the golden-path browser smoke headless.

## Execution model

- Each milestone dispatches 4–5 agents in parallel.
- Between milestones: user tests, gives feedback, feeds into next
  milestone's scope.
- ~5 review checkpoints total across all milestones.

## Deferred

- **Auth** — its own project, out of current scope since the demo is
  local-only. Tracked separately.
