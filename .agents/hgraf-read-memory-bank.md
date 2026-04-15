---
name: hgraf-read-memory-bank
description: Navigating docs/, AGENTS.md, and the iter16 doc set to get context on any harmonograf subsystem before making changes.
---

# hgraf-read-memory-bank

## When to use

You are starting work on a harmonograf task and need orientation on a subsystem you haven't touched. Instead of grep-walking the code from scratch, read the curated documents in the right order. This skill is an index to the "memory bank" — the human-written docs that explain *why* the code looks the way it does.

Use this as the first 5-10 minutes of any non-trivial task.

## Prerequisites

None. All files referenced are in-repo.

## The reading order

### Layer 1 — project-level (always read first, < 5 minutes)

1. **`AGENTS.md`** — project vision + high-level architecture + the *Plan execution protocol* section. The protocol section is load-bearing: it explains the three coordinated channels (session.state / reporting tools / ADK callback inspection) and the three orchestration modes (Sequential / Parallel / Delegated). If you skip this you will end up fighting the state machine.
2. **`README.md`** — external framing. Usually less useful than AGENTS.md but occasionally has setup quirks that the dev docs omit.
3. **`docs/operator-quickstart.md`** — how to boot the stack, the prompt you drive, what you should see. Complement to the `hgraf-run-demo` skill.
4. **`docs/reporting-tools.md`** — full reference for the reporting tools (see `hgraf-add-reporting-tool`). Read this before touching anything in `client/harmonograf_client/tools.py` or `before_tool_callback`.

### Layer 2 — subsystem docs (pick based on task)

`docs/design/` contains numbered design documents. Start with the one closest to your task area:

- **`01-data-model-and-rpc.md`** — the shared data model (agent/span/task/plan/annotation) and the proto-level RPC shape. Required reading before `hgraf-add-proto-field`.
- **`02-client-library.md`** — the pre-ADK design of the client library. Historical but still accurate for the transport layer.
- **`03-server.md`** — server architecture intent (pre-ingest refactor). Read alongside `11-server-architecture.md`.
- **`04-frontend-and-interaction.md`** — early UX intent. Read alongside `10-frontend-architecture.md` and `13-human-interaction-model.md`.
- **`10-frontend-architecture.md`** — the **current** Gantt + shell architecture. This is the authoritative reference for frontend changes. Cross-reference with `hgraf-update-frontend-component` and `hgraf-add-gantt-overlay`.
- **`11-server-architecture.md`** — the **current** server architecture (ingest / bus / storage / control router). Authoritative for `server/harmonograf_server/` edits.
- **`12-client-library-and-adk.md`** — the **current** client architecture including the ADK adapter, the `_AdkState` design, and why `adk.py` is 5700 lines. **Mandatory reading** before touching `adk.py` — see `hgraf-safely-modify-adk-py`.
- **`13-human-interaction-model.md`** — the "what does a user actually do with this console" document. Read when designing new UI affordances.
- **`14-information-flow.md`** — the end-to-end data path: agent emits → client buffers → server ingests → bus fans out → frontend renders. Best single document for understanding a bug that crosses component boundaries.

### Layer 3 — research + rationale

- **`docs/research/hci-orchestration-paper.md`** — the HCI paper that informs the console's interaction model. Read when deciding whether a new feature is aligned with the project's theoretical grounding (e.g. "should this be pull-based or push-based").

### Layer 4 — milestones + changelog

- **`docs/milestones.md`** — the sequence of iteration goals. Useful to understand "why was X merged in iter12 with a rough edge that iter14 cleaned up." Check this before rewriting something — the history is often why it is what it is.

## What to read for common tasks

| Task | Read in this order |
|---|---|
| Add drift kind | AGENTS.md (protocol section) → `12-client-library-and-adk.md` → `10-frontend-architecture.md` → `driftKinds.ts` |
| Add proto field | `01-data-model-and-rpc.md` → `11-server-architecture.md` → `12-client-library-and-adk.md` |
| Modify ingest path | `11-server-architecture.md` → `14-information-flow.md` → `server/harmonograf_server/ingest.py` |
| Add Gantt overlay | `10-frontend-architecture.md` → `14-information-flow.md` → existing ContextWindowSample implementation |
| Modify orchestration | AGENTS.md (protocol section) → `12-client-library-and-adk.md` → `adk.py` (see `hgraf-safely-modify-adk-py`) |
| Debug stuck task | AGENTS.md → `hgraf-debug-task-stuck` skill → `invariants.py` |
| Design new UX | `13-human-interaction-model.md` → `docs/research/hci-orchestration-paper.md` |

## Cross-references to code ground truth

When the docs and the code disagree, **the code is the ground truth** (docs drift between iterations). Anchor your reading with these key files:

- Protocol constants: `client/harmonograf_client/state_protocol.py:86-126` (KEY_ names + ALL_KEYS)
- Orchestration modes: `client/harmonograf_client/agent.py:207-470` (HarmonografAgent class)
- State machine: `client/harmonograf_client/adk.py:242-280` (_set_task_status) + `invariants.py:41-55` (allowed transitions)
- Drift taxonomy: `client/harmonograf_client/adk.py:326-378` + `frontend/src/gantt/driftKinds.ts:30-58`
- Bus deltas: `server/harmonograf_server/bus.py:25-65`
- SQLite schema: `server/harmonograf_server/storage/sqlite.py:45-170`
- Gantt renderer: `frontend/src/gantt/renderer.ts:1-97`

## Verification

You know you have read enough when you can answer:
1. Which of the three orchestration modes does your task apply to?
2. Which of the three drift detection paths (detect_drift / after_model_callback / before_tool_callback) does your signal route through?
3. Which `DELTA_*` kind does your change emit, if any?
4. Which sqlite table does your change read or write, if any?
5. Which frontend store subscribes to the resulting update?

If any answer is "I don't know," re-read the relevant design doc before writing code.

## Common pitfalls

- **Skipping AGENTS.md because "I already read it."** The protocol section changes with iterations. Re-skim it if you haven't touched the project in a month.
- **Trusting `02-client-library.md` over `12-client-library-and-adk.md`.** The early design docs describe intent; the later ones describe reality. When they disagree, the higher-numbered one wins.
- **Reading `milestones.md` as authoritative.** It is a log, not a spec. Useful for archaeology, not for deciding current behavior.
- **Ignoring code when docs are clear.** Even the current design docs lag the code by ~1 iteration. Always cross-check against the files listed above before making assumptions.
- **Leaving the memory bank unchanged after a large PR.** If you land something that invalidates a doc, fix the doc in the same PR. Otherwise the next agent reads stale context and repeats your investigation. See task #5-#8 in the original milestone docs for how the docs have been maintained historically.
- **Expecting skills to replace docs.** The skills in `.claude/skills/` are task recipes. The docs in `docs/design/` are architecture. Read docs for "why," read skills for "how."
