---
name: hgraf-update-planner-prompt
description: Change the planner/refine system prompts in planner.py safely — schema contract, test against real LLM, regression guards.
---

# hgraf-update-planner-prompt

## When to use

You want to change how harmonograf's PlannerHelper decomposes a request into a task DAG, or how the Refine path rewrites a plan in response to drift. The prompts are the LLM's *entire* contract — changing a word can shift schema adherence, task granularity, or drift-response behavior.

## Prerequisites

1. Read the full planner module: `client/harmonograf_client/planner.py`. The two prompts you will edit:
   - `_DEFAULT_SYSTEM_PROMPT` at `planner.py:164` — initial plan generation.
   - `_REFINE_SYSTEM_PROMPT` at `planner.py:408` — live refine path.
2. Read the JSON schema both prompts reference. The response shape is parsed in `planner.py :: _plan_from_json` (around `planner.py:230-290`). The prompt MUST match what the parser expects, or every refine returns `None` and the drift triggers silently.
3. Read `client/harmonograf_client/adk.py :: _refine_plan_async` and follow `plan_state.plan.revision_index` bumping around `adk.py:3372` and `adk.py:3486` — those are the insertion points where a new refined plan is stored.
4. Read the existing planner tests: `client/tests/test_planner.py` for the generate path and `client/tests/test_dynamic_plans_real_adk.py` for the end-to-end refine path with a real ADK runner and scripted `FakeLlm`.

## Step-by-step

### 1. Identify which prompt you're changing

- **Initial plan**: edit `_DEFAULT_SYSTEM_PROMPT`. This runs once at session start via `PlannerHelper.generate()`.
- **Refine**: edit `_REFINE_SYSTEM_PROMPT`. This runs every time a drift kind fires (`adk.py :: DriftReason`) inside `PlannerHelper.refine()` (`planner.py:394`).
- **Override from the application**: don't edit the module at all. `PlannerHelper(system_prompt=...)` at `planner.py:310` accepts a custom string. If you're experimenting, pass a test prompt rather than committing to the repo prompt.

### 2. Preserve the JSON schema contract

Every prompt ends with a literal schema block. Don't change field names. The parser looks for exactly these keys:

Initial plan:
```json
{"summary": "...", "tasks": [{"id","title","description","assignee_agent_id"}], "edges": [{"from_task_id","to_task_id"}]}
```

Refine plan (adds a `status` field per task):
```json
{"summary": "...", "tasks": [{"id","title","description","assignee_agent_id","status"}], "edges": [{"from_task_id","to_task_id"}]}
```

`_plan_from_json` will drop the response if any required key is missing. Since it returns `None`, the refine path silently keeps the old plan. Add a unit test that asserts your new prompt still parses.

### 3. Keep the stability rules intact

The refine prompt has seven numbered rules at `planner.py:417-448` — `PRESERVE HISTORY`, `UPDATE STATUSES`, `ADD NEW TASKS`, `DROP OBSOLETE PENDING`, `REASSIGN`, `KEEP IDS STABLE`, `RETURN A COMPLETE PLAN`. These are load-bearing.

- Dropping `PRESERVE HISTORY` makes the Gantt lose completed tasks — the UI shows a broken timeline.
- Dropping `KEEP IDS STABLE` breaks the revision diff (`frontend/src/gantt/index.ts :: computePlanDiff`) which depends on id-based matching.
- Dropping `RETURN A COMPLETE PLAN` breaks `adk.py :: _upsert_refined_plan` which does a wholesale replace, not a merge.

If you need to weaken one of these, change the parser and the adapter atomically — not just the prompt.

### 4. Keep the "no prose, no markdown fences" instruction

Both prompts end with "Respond with a single JSON object and NOTHING ELSE — no prose, no markdown fences". `_strip_code_fences` at `planner.py:227` handles fenced JSON as a fallback, but leading prose breaks the parser. If you remove this instruction, LLMs will preface with "Here is the plan:" and everything silently fails.

### 5. Test with a scripted FakeLlm

The repeatable path: scripted `FakeLlm` replay. See `client/tests/test_dynamic_plans_real_adk.py:129` for the pattern:

```python
class FakeLlm(BaseLlm):
    model: str = "fake-llm"
    responses: list = []
    cursor: int = -1
    @classmethod
    def supported_models(cls): return ["fake-llm"]
    async def generate_content_async(self, llm_request, stream=False):
        self.cursor += 1
        yield self.responses[min(self.cursor, len(self.responses)-1)]
```

Enqueue a sequence that exercises every rule you changed: an initial plan, then a drift event, then the refined plan. Assert:

- The returned `Plan` parses (`_plan_from_json` returns non-None).
- `revision_index` incremented by 1.
- Completed tasks still appear in the refined plan.
- New tasks have fresh ids.

### 6. Test with a real LLM

For prompt changes, unit tests aren't enough — you need the actual model's behavior. Two options:

**Option A — Integration test with API key**:

```bash
GOOGLE_API_KEY=... uv run pytest client/tests/test_dynamic_plans_real_adk.py::test_real_llm -m real_llm -x
```

(If `-m real_llm` marker doesn't exist, grep the tests for the actual marker name. The suite uses skip markers to keep API-key-gated tests out of CI.)

**Option B — Manual REPL**:

```python
from harmonograf_client.planner import PlannerHelper, make_default_adk_call_llm
ph = PlannerHelper(call_llm=make_default_adk_call_llm(), model="gemini-2.5-flash")
plan = ph.generate(user_request="...", available_agents=[{"id": "researcher", "name": "..."}])
print(plan)
```

Run 5-10 prompt variants against it. Count how many produce a parseable `Plan`. A drop from 95% to 80% is a prompt regression, even if unit tests pass.

### 7. Measure DAG shape regressions

After a prompt change, refined plans may become too small (the LLM dropped rules), too large (LLM over-decomposes), or too linear (LLM stopped producing parallel branches). Spot-check by running the planner on three representative requests and comparing task count, edge count, and max DAG width against the baseline.

### 8. Update refine throttling if behavior changes

The refine path is throttled per drift kind at `adk.py:3283` — "Recoverable drifts within the throttle window don't re-fire refine". If your prompt change makes refine much cheaper or much more expensive, retune `_REFINE_THROTTLE_BY_KIND` in `adk.py:375` (the table above `_last_refine_by_kind`).

### 9. Verification

```bash
uv run pytest client/tests/test_planner.py -x -q
uv run pytest client/tests/test_dynamic_plans_real_adk.py -x -q
# Only if you have API access:
GOOGLE_API_KEY=... uv run pytest client/tests/test_dynamic_plans_real_adk.py -x -q -k real_llm
```

## Common pitfalls

- **Adding a required field to the schema**: the parser silently drops the plan, the refine silently no-ops, and the only symptom is a stuck Gantt chart. Always update `_plan_from_json` atomically with the prompt.
- **Quoting drift**: copy-pasting JSON from a Markdown editor introduces curly quotes (`"` instead of `"`). LLMs mirror them back and the parser rejects the response. Hand-type or use a plain editor.
- **"Respond with only JSON" without the "no markdown fences"**: Gemini and Claude will still wrap responses in ```json```. `_strip_code_fences` handles it; don't remove the guard.
- **Changing the prompt without changing the tests**: the old scripted `FakeLlm` responses will keep passing because the LLM is fake. Add a new test case that specifically exercises the changed behavior.
- **Forgetting that refine has state**: the refine prompt receives the current plan AS JSON. If your prompt change also changes how the plan is serialized into the request, that's a second edit — search `adk.py` for the `current plan` serialization path.
- **Dropping "complete plan, not a delta"**: one-word removal breaks the upsert — the plan-replacement path expects the whole thing. Never hint the model to "return only changes".
