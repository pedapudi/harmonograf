# Runbook: Plan never gets submitted

User sent their first message, the agent accepted it, but no plan is
rendered in the UI. The Gantt stays empty; `task_plans` is empty in
sqlite; the drawer's Plan tab is blank.

**Triage decision tree** — the SKIP-reason log lines are the fast path.
Grep `maybe_run_planner: SKIP` first; the `reason=...` value names the branch.

```mermaid
flowchart TD
    Start([No plan rendered]):::sym --> Q1{Client log:<br/>maybe_run_planner: SKIP?}
    Q1 -- "reason=no_planner" --> F1[Construct HarmonografAgent<br/>with planner=LLMPlanner(...)]:::fix
    Q1 -- "reason=is_harmonograf_agent<br/>_without_host" --> F2[Sub-agent: expected.<br/>Or set orchestrator_mode=True]:::fix
    Q1 -- "no SKIP line" --> Q2{planner.generate<br/>raised?}
    Q2 -- "yes" --> F3[Fix LLM connectivity /<br/>prompt — see demo-wont-start]:::fix
    Q2 -- "no" --> Q3{LLMPlanner: empty<br/>response?}
    Q3 -- "yes" --> F4[Model returned function call:<br/>switch to structured output]:::fix
    Q3 -- "no" --> Q4{client.submit_plan<br/>raised?}
    Q4 -- "yes" --> F5[Transport problem:<br/>see agent-disconnects-repeatedly]:::fix
    Q4 -- "no" --> Q5{no user request<br/>found on invocation?}
    Q5 -- "yes" --> F6[Tool-triggered turn,<br/>not user turn — no plan by design]:::fix
    Q5 -- "no" --> F7[Custom planner hook raised:<br/>grep 'planner hook raised']:::fix

    classDef sym fill:#fde2e4,stroke:#c0392b,color:#000
    classDef fix fill:#d4edda,stroke:#27ae60,color:#000
```

## Symptoms

- **UI**: current-task strip reads "no plan yet" or similar; no task
  rows under the agent's swimlane.
- **Client log** (with `LOG_LEVEL=DEBUG` on `harmonograf_client.adk`):
  - `DEBUG harmonograf_client.adk: maybe_run_planner: SKIP reason=is_harmonograf_agent_without_host inv_id=...`
    (`adk.py:1884`)
  - `DEBUG harmonograf_client.adk: maybe_run_planner: SKIP reason=no_planner inv_id=...`
    (`adk.py:1891`)
  - `DEBUG harmonograf_client.adk: planner: no user request found on invocation ...`
    (`adk.py:1917`)
  - `WARN harmonograf_client.adk: planner.generate raised; skipping plan: <exc>`
    (`adk.py:1954`)
  - `WARN harmonograf_client.planner: LLMPlanner: call_llm raised <exc>; skipping plan`
    (`planner.py:345`)
  - `WARN harmonograf_client.planner: LLMPlanner: empty/non-string LLM response; skipping plan`
    (`planner.py:348`)
  - `DEBUG harmonograf_client.adk: planner: no plan produced for invocation ...`
    (`adk.py:1957`)
  - `WARN harmonograf_client.adk: client.submit_plan raised; ignoring: <exc>`
    (`adk.py:1978`)

## Immediate checks

```bash
# Fast path — grep for the SKIP reasons first. They tell you
# exactly which branch short-circuited.
grep -E 'maybe_run_planner: SKIP|no user request found|no plan produced|planner.generate raised|LLMPlanner:' /path/to/agent.log | tail -30

# Is there any plan in sqlite at all for this session?
sqlite3 data/harmonograf.db \
  "SELECT id, revision_index, revision_kind FROM task_plans
   WHERE session_id='SESSION_ID' ORDER BY revision_index DESC LIMIT 5;"

# Is the LLM reachable? (if you're pointed at a local OpenAI-compatible model)
curl -s "$OPENAI_API_BASE/models" || echo "no models endpoint"
```

## Root cause candidates (ranked)

1. **`reason=no_planner`** — the `HarmonografAgent` was constructed
   without a `planner=...` argument, or the planner attribute was
   cleared. `adk.py:1891`. No planner, no plan — by design.
2. **`reason=is_harmonograf_agent_without_host`** — the embedded
   agent is running under ADK with no host coordinator, and the
   adapter has decided not to plan. `adk.py:1884`. Typical when
   running a sub-agent directly.
3. **Planner raised an exception** — `planner.generate` threw; the
   adapter logged `planner.generate raised; skipping plan` and moved on
   (`adk.py:1954`). Usually a malformed prompt or an LLM connectivity
   failure.
4. **LLM returned an empty / non-string response** —
   `LLMPlanner: empty/non-string LLM response; skipping plan`
   (`planner.py:348`). Happens when the model returns a structured
   response that doesn't pass the planner's parser.
5. **Planner returned a plan but `submit_plan` raised** —
   `client.submit_plan raised; ignoring: <exc>` (`adk.py:1978`). The
   plan existed but couldn't be pushed to the server. Usually a
   transport problem or a proto validation error.
6. **No user text found on the invocation** —
   `planner: no user request found on invocation <inv_id>`
   (`adk.py:1917`). The invocation carried no user content for the
   planner to plan against, often because the request came via a
   tool-call loop rather than a fresh user turn.
7. **Planner hook silently swallowed** —
   `planner hook raised; ignoring: <exc>` (`adk.py:1172`). A user-provided
   hook intercepted the plan and threw.

## Diagnostic steps

### 1. `no_planner`

Search your agent construction site:

```bash
grep -rn 'HarmonografAgent(' /path/to/agent_src/
```

Does it pass `planner=...`? If not, that's the cause. The adapter does
not invent a planner.

### 2. `is_harmonograf_agent_without_host`

```bash
grep -rn 'run_as_host\|orchestrator_mode' /path/to/agent_src/
```

If the agent is intentionally a sub-agent, this is expected behaviour
and there's nothing to fix at the adapter level — the host is meant to
plan.

### 3. Planner exception

```bash
grep -A 3 'planner.generate raised' /path/to/agent.log
```

The `<exc>` message is usually a `ConnectionError`, `TimeoutError`, or
a JSON parse error.

### 4. Empty LLM response

```bash
grep -B 2 -A 2 'LLMPlanner: empty/non-string' /path/to/agent.log
```

The lines above the warning will show the request shape. If the model
returned a function call instead of text, the planner's parser won't
handle it.

### 5. `submit_plan` raised

```bash
grep 'client.submit_plan raised' /path/to/agent.log
```

Then investigate the transport state — see
[`agent-disconnects-repeatedly.md`](agent-disconnects-repeatedly.md).

### 6. No user request

Check ADK flow: was the invocation triggered by a tool call, not a
user turn? Harmonograf only plans off user text.

### 7. Planner hook

```bash
grep 'planner hook raised' /path/to/agent.log
```

Exception message identifies the hook.

## Fixes

1. **no_planner**: construct `HarmonografAgent(planner=LLMPlanner(...))`
   or equivalent, passing an explicit planner instance.
2. **is_harmonograf_agent_without_host**: run the agent as part of a
   host, or set `orchestrator_mode=True` explicitly if you want the
   adapter to own planning.
3. **Planner exception**: fix the LLM connectivity (see
   [`demo-wont-start.md`](demo-wont-start.md) for `KIKUCHI_LLM_URL`);
   or fix the prompt shape that made `generate` throw.
4. **Empty response**: make the planner prompt more prescriptive, or
   switch to a structured-output mode.
5. **submit_plan raised**: fix transport
   ([`agent-disconnects-repeatedly.md`](agent-disconnects-repeatedly.md)).
6. **No user text**: you probably don't want a plan on this invocation.
   Either enable auto-planning on tool-triggered turns (future work),
   or accept that planning happens only on user turns.
7. **Planner hook**: fix the hook; or remove it and let the default
   planner run.

## Prevention

- In unit tests, assert that every `HarmonografAgent` constructed in
  production code either has a planner or is explicitly marked as a
  sub-agent.
- Add a metric on `plan_submissions_per_session` and alert on zero.

## Cross-links

- [`runbooks/plan-revisions-not-appearing.md`](plan-revisions-not-appearing.md)
  — similar class of bug but on the *refine* path.
- [`design/planner.md`](../design/planner.md) if present, or
  [`dev-guide/client-library.md`](../dev-guide/client-library.md).
- Code: `client/harmonograf_client/adk.py:1884-1978` and
  `client/harmonograf_client/planner.py:345-396`.
