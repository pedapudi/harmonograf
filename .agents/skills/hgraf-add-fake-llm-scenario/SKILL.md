---
name: hgraf-add-fake-llm-scenario
description: Script a complex multi-turn FakeLlm scenario — function calls, tool chains, drift events, side-effect hooks, partial responses — for deterministic adk test coverage.
---

# hgraf-add-fake-llm-scenario

## When to use

You are writing a test that needs a deterministic sequence of LLM turns, possibly interleaved with tool-call replies, transfers, drift triggers, or control events.

## Post-goldfive-migration scope

Harmonograf's dedicated FakeLlm harnesses moved out with the rest of
the orchestration code. The current places to look for reusable
FakeLlm patterns:

- `tests/e2e/` — harmonograf's own end-to-end tests. Look for a
  `FakeLlm` or similar fixture colocated with the scenario.
- `third_party/goldfive/tests/` — goldfive's FakeLlm scaffolding,
  which is what you actually want for scripted-turn tests that
  exercise the planner / steerer / drift detectors.

The two building blocks are the same:

- `LlmResponse(content=Content(parts=[Part(text=...)]))` for plain
  text turns.
- `LlmResponse(content=Content(parts=[Part(function_call=FunctionCall(...))]))`
  for tool calls.

Both types come from `google.adk.models.llm_response` and
`google.genai.types`.

## Scenario-building primitives

### Plain text turn

```python
from client.tests.test_dynamic_plans_real_adk import _text
responses.append(_text("I'll call the search tool next."))
```

### Function call turn

```python
from client.tests.test_dynamic_plans_real_adk import _fc
responses.append(_fc("search_web", {"query": "climate change 2026"}))
```

ADK's runner then sends the function call downstream, expects a matching function-response, and fires the `_on_tool_callback` path in `_AdkState`. Your next `responses` entry is whatever the LLM says *after* receiving the tool result.

### Side-effect hook between turns

The FakeLlm dispatcher at `test_dynamic_plans_real_adk.py:155` has a special behavior: if a response entry is a `callable`, it runs the callable (cursor++), then falls through to the next entry for the actual LLM reply.

```python
def fire_drift():
    _adk_state.refine_plan_on_drift(hsession_id, DriftReason(kind="tool_error"))

responses = [
    _text("initial"),
    fire_drift,  # side effect runs before the next turn's response
    _text("post-drift reply"),
]
```

Use this for: delivering control events mid-run, mutating `session.state`, firing drifts, injecting stale plan state.

### Multi-agent handoff

To exercise a transfer, emit a function call to the `transfer_to_agent` tool:

```python
responses.append(_fc("transfer_to_agent", {"agent_name": "sub_agent"}))
```

The runner picks this up, transfers control, and the sub-agent's LlmAgent (backed by a *second* FakeLlm) starts consuming its own `responses` queue. Each FakeLlm instance is independent — one per agent.

### Tool-call chain

For a sequence like `call_a → result_a → call_b → result_b → final_text`:

```python
responses = [
    _fc("tool_a", {"input": "x"}),
    # ADK feeds tool_a's result back
    _fc("tool_b", {"input": "y"}),
    # ADK feeds tool_b's result back
    _text("Done: result combines a and b."),
]
```

The harness advances cursor every `generate_content_async` call, so each tool turn consumes one slot.

### Partial / streaming response

Real LLMs yield multiple `LlmResponse` objects per call when streaming. The FakeLlm in `test_dynamic_plans_real_adk.py` yields exactly one per call. If you need streaming semantics:

```python
class StreamingFakeLlm(FakeLlm):
    async def generate_content_async(self, llm_request, stream=False):
        self.cursor += 1
        item = self.responses[min(self.cursor, len(self.responses)-1)]
        if isinstance(item, list):
            for chunk in item:
                yield chunk
        else:
            yield item
```

Then a streaming turn is a `list` of chunked `LlmResponse` objects: `[_text("partial "), _text("response complete")]`.

### Injecting state_delta writes

Harmonograf's `on_event_callback` looks for `state_delta` events (`adk.py`). To simulate an agent writing to `session.state`:

```python
responses.append(_fc("update_state", {"key": "harmonograf.task_progress", "value": "50%"}))
```

Then add a local `update_state` tool that writes to `session.state` and returns a confirmation. ADK will turn that into a state_delta event and harmonograf will observe it in the callback.

### Testing drift-kind coverage

For each drift kind in `DriftReason`, build a scenario that specifically triggers it. Examples:

- `tool_error` — LLM emits function call, tool handler raises.
- `llm_refusal` — LLM emits `_text("I can't help with that.")`.
- `new_work_discovered` — LLM calls `report_new_work_discovered(description=...)`.
- `plan_divergence` — LLM calls `report_plan_divergence(...)`.
- `wrong_agent` — LLM transfers to an agent not in the plan.
- `multiple_stamp_mismatches` — emit more tool calls for already-COMPLETED tasks than `_STAMP_MISMATCH_THRESHOLD` (`adk.py:373`).

The canonical inventory of drift kinds is `client/tests/test_drift_taxonomy.py`. Each drift has a scripted scenario there you can copy.

## Step-by-step scenario write

### 1. Decide the expected transcript

Write it out in comments first:

```
# Turn 1: LLM asks to search → tool_a call
# Turn 2: LLM receives tool_a result, asks to summarize → tool_b call
# Turn 3: LLM receives tool_b result, fires drift (task already complete)
# Turn 4: refined plan arrives
# Turn 5: LLM continues with new task
```

### 2. Build the `responses` list in order

Keep one entry per `generate_content_async` call. Side-effect hooks (callables) are cursor-invisible — they advance cursor inside the dispatcher but don't count as turns from ADK's perspective.

### 3. Hook the InMemoryRunner

```python
from google.adk.runners import InMemoryRunner
from google.adk.agents.llm_agent import LlmAgent
runner = InMemoryRunner(agent=LlmAgent(name="root", model=fake_llm_instance, tools=[...]))
plugin = make_adk_plugin(client, planner=StaticPlanner(Plan(...)))
# plug-in install via runner.plugins or via root agent's plugins field.
```

`StaticPlanner` avoids real planner LLM calls — feed a canned plan via `Plan(tasks=[...], edges=[...])`.

### 4. Run the invocation

```python
async for event in runner.run_async(session_id="sess_test", user_id="u", new_message=...):
    pass  # drive the runner to completion
```

### 5. Assert on outcomes

Check the relevant surfaces after the run:

- `client._events` or `FakeClient.calls` — spans emitted.
- `_adk_state._plan_states[hsession_id]` — task statuses, revision index.
- `_adk_state._metrics` — callback counters, drift counts.
- `client._control_acks` — if you delivered control events via side-effect hooks.

### 6. Avoid over-scripting

A common mistake: writing 50-turn scenarios that break on any ADK change. Prefer 3–5 turn scenarios, each focused on exactly one invariant. Compose them as separate test cases.

## Verification

```bash
uv run pytest client/tests/test_dynamic_plans_real_adk.py -x -q
uv run pytest client/tests/test_drift_taxonomy.py -x -q
uv run pytest client/tests/test_llm_agency_scenarios.py -x -q
```

## Common pitfalls

- **Cursor off-by-one with side-effect hooks**: a `callable` entry advances the cursor BEFORE falling through, so the next actual response is two slots later. Double-check when mixing callables and responses.
- **Forgetting a response after a function call**: ADK feeds the function-response back and expects another LLM turn. If you run out of responses, the FakeLlm falls back to its final response — which is usually fine but can silently mask bugs.
- **Tool-name typos**: `_fc("search_eweb", ...)` with a typo matches no real tool. ADK fires a tool-error path you weren't testing. Assert tool names against `tool.__name__`.
- **Sharing FakeLlm across agents**: each agent in a multi-agent scenario gets its own FakeLlm instance with its own responses. Sharing one collapses cursors together unpredictably.
- **Expecting `session.state` writes to happen synchronously**: state_delta events flow through the event queue on the next tick. Await the event loop or assert after the run finishes.
- **Test leaking across runs**: InMemoryRunner + module-level `_adk_state` carries across tests. Use fresh fixtures per test — conftest reset helpers live in `client/tests/_fixtures.py`.
