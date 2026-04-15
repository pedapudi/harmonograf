---
name: hgraf-add-reporting-tool
description: Add a new reporting tool end-to-end — tools.py definition, before_tool_callback interception, state_protocol key, frontend OrchestrationTimeline icon, and instruction appendix update.
---

# hgraf-add-reporting-tool

## When to use

The existing reporting tools (`report_task_started`, `report_task_progress`, `report_task_completed`, `report_task_failed`, `report_task_blocked`, `report_new_work_discovered`, `report_plan_divergence` — defined in `client/harmonograf_client/tools.py:77-165`) cover the standard task lifecycle + divergence signals. You need a new tool when:
- A new *kind of report* the agent should volunteer (not a drift the server infers).
- A new shared-state field the agent should push (e.g. "I am waiting on external human approval").
- A new hook for the Gantt timeline (e.g. "begin experiment phase" checkpoint).

Do **not** add a reporting tool when the equivalent can be done via an existing `harmonograf.*` state key. The reporting tools should remain a small, curated surface — every one of them is injected into every sub-agent and eats tokens in system instructions.

## Prerequisites

1. Read the full reporting-tools reference at `docs/reporting-tools.md` and the protocol section of `AGENTS.md`.
2. Read `client/harmonograf_client/tools.py` end-to-end (it is only ~200 lines) so you understand the `REPORTING_TOOL_FUNCTIONS` tuple, `build_reporting_function_tools()`, and `SUB_AGENT_INSTRUCTION_APPENDIX`.
3. Read `client/harmonograf_client/state_protocol.py` — specifically the constants at lines 89-103 and the ALL_KEYS tuple at 112-126.

## Step-by-step

### 1. Define the tool function in tools.py

Add a new function to `client/harmonograf_client/tools.py` matching the pattern of the existing tools (lines 77-165). The body **must** return `{"acknowledged": True}` — the real work happens in `before_tool_callback` interception. Example:

```python
def report_awaiting_human_approval(
    task_id: str,
    reason: str,
    expected_resolver: str = "",
) -> dict:
    """Signal that the current task is blocked on out-of-band human approval.

    Harmonograf intercepts this call in before_tool_callback and transitions
    the task to BLOCKED with a structured reason. Returns {"acknowledged": True}.
    """
    return {"acknowledged": True}
```

Docstring is critical — ADK surfaces it to the model as part of the tool schema.

### 2. Register in the tuples

Same file, around lines 168-180:

```python
REPORTING_TOOL_FUNCTIONS = (
    report_task_started,
    ...
    report_awaiting_human_approval,   # new
)

REPORTING_TOOL_NAMES = tuple(f.__name__ for f in REPORTING_TOOL_FUNCTIONS)
```

`build_reporting_function_tools()` at lines 183-190 wraps each into a `google.adk.tools.FunctionTool` — no edits needed.

### 3. Update the instruction appendix

`SUB_AGENT_INSTRUCTION_APPENDIX` at `tools.py:193-202` is appended to every sub-agent's instruction (via `HarmonografAgent._auto_register_reporting_tools` at `agent.py:335-355`). Add a one-sentence bullet describing when to call your tool. Keep it terse — every word here is replicated across every agent and multiplies token cost.

### 4. Add the interception in before_tool_callback

`client/harmonograf_client/adk.py:1299-1316` is `async def before_tool_callback`. The existing pattern dispatches on tool name via `_dispatch_reporting_tool`. Find that function (grep for `_dispatch_reporting_tool`) and add a branch for your new tool name.

Inside the branch:
- Parse arguments out of the `args` dict.
- Apply state via `_AdkState` — **always** through `_set_task_status()` at `adk.py:242-280` for status transitions, never by mutating `task.status` directly. See `hgraf-safely-modify-adk-py`.
- If the tool should produce a `DriftReason`, construct one and route it through the same refine path `report_task_failed` and `report_plan_divergence` use.

### 5. Add a state_protocol key (if applicable)

If your tool writes a new field readable by other agents, add a `KEY_*` constant to `client/harmonograf_client/state_protocol.py` (alongside lines 99-103 for Agents→Harmonograf keys):

```python
KEY_AWAITING_HUMAN = HARMONOGRAF_PREFIX + "awaiting_human"
```

Add it to the `ALL_KEYS` tuple at lines 112-126. Wire the interception in step 4 to write this key into `session.state` so other sub-agents can read it. Use `_safe_get()` / `_safe_str()` at lines 129-144 for reads — they never raise.

### 6. Add unit tests

Tests live in `client/tests/test_reporting_tools.py` and `client/tests/test_reporting_registration.py`. Follow the existing patterns:

- `test_reporting_registration.py` — assert your new function is in `REPORTING_TOOL_FUNCTIONS`, that `REPORTING_TOOL_NAMES` includes it, and that `build_reporting_function_tools()` produces a FunctionTool for it.
- `test_reporting_tools.py` — construct a fake ADK event stream invoking the tool, assert `_AdkState` applies the expected transition and state keys.
- `test_state_protocol.py` — if you added a new KEY_*, assert it round-trips through the safe getters.

```bash
cd client && uv run --with pytest --with pytest-asyncio python -m pytest \
  tests/test_reporting_tools.py tests/test_reporting_registration.py tests/test_state_protocol.py -q
```

### 7. Frontend: OrchestrationTimeline icon

The orchestration timeline in the drawer (`frontend/src/components/shell/OrchestrationTimeline.tsx:7-87`) renders reporting-tool invocations as timeline entries. Find `KIND_LABEL` and `ALL_KINDS` near the top of that file — add a new entry for your tool name, pick a label and icon.

The tool name flows from span attributes (set by `before_tool_callback`) through the ingest pipeline into the frontend `TOOL_CALL` span renderer. If you picked a brand new name, the timeline will show `Unknown` until you add the mapping here.

### 8. Documentation

Update `docs/reporting-tools.md` with a reference entry for the new tool. Keep the structure consistent with the existing entries (signature, when to call, state it writes, example).

## Verification

```bash
# 1. client tests
cd client && uv run --with pytest --with pytest-asyncio python -m pytest tests/test_reporting_tools.py tests/test_reporting_registration.py tests/test_state_protocol.py tests/test_tool_callbacks.py -q

# 2. full client suite
cd client && uv run --with pytest --with pytest-asyncio python -m pytest -q

# 3. frontend
cd frontend && pnpm lint && pnpm build

# 4. live smoke — drive a scenario where an agent calls the new tool
make demo
```

In the demo UI: open the Drawer → Orchestration Timeline → confirm the new tool appears with your label and icon, and the task row reflects the expected state transition.

## Common pitfalls

- **Tool body does real work.** The body **must** return `{"acknowledged": True}` only. The real effect happens in `before_tool_callback` interception so that `_AdkState` is the single writer to task status. If you do work in the tool body, you bypass the state-machine lock and create race conditions under `parallel_mode=True`.
- **Auto-registration is subtree-wide.** `agent.py:335-355` walks the full sub-agent tree at `HarmonografAgent` construction and appends your tool to every `LlmAgent`. Every agent pays the system-prompt cost of your new tool. Be parsimonious with the tools you add and with the instruction appendix prose.
- **Instruction appendix bloat.** Each new bullet multiplies across every agent's instruction. Aim for ≤15 words per bullet. Prefer examples in `docs/reporting-tools.md` over verbose in-prompt instructions.
- **Not going through `_set_task_status`.** Direct `task.status = ...` assignments skip the monotonic-transition guard at `adk.py:242-280`. The invariant checker (`invariants.py`) will flag the resulting state on the next sweep and log a violation, but by then you've already written wrong state to the server.
- **Forgetting ALL_KEYS.** If you add a `KEY_*` constant but don't add it to `ALL_KEYS` at `state_protocol.py:112-126`, the diff helper will silently not pick it up, and the delta stream to the server will miss writes.
- **Frontend mapping lag.** You can ship client-only and the tool will still work — the timeline just shows a generic `Unknown` entry. Many new tools get stuck in that state because nobody goes back and adds the mapping. Do both sides in the same PR.
- **Wire name ≠ function name.** `REPORTING_TOOL_NAMES` is derived from `__name__`. Renaming the function renames the wire key and breaks any persisted sqlite rows or replayed traces referencing the old name. Pick the name once.
