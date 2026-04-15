# Runbook: Drift not firing

A tool failed, the agent refused, or the user sent a steer — and
nothing happened. No `drift observed` log line, no refine, no plan
revision.

**Triage decision tree** — drift detection runs from ADK callbacks; if the
right callback never fires for the event class, the detector is blind.

```mermaid
flowchart TD
    Start([Expected drift,<br/>nothing detected]):::sym --> Q1{ADK callback fired<br/>at all? before/after_tool,<br/>on_event}
    Q1 -- "no" --> F1[ADK version skipped<br/>callback for this event;<br/>upgrade ADK or add hook]:::fix
    Q1 -- "yes" --> Q2{Tool actually raised<br/>or returned error obj?}
    Q2 -- "returned obj" --> F2[Tool swallowed exception:<br/>let it propagate so<br/>on_tool_end sees error=]:::fix
    Q2 -- "raised" --> Q3{detect_drift raised<br/>and was swallowed?<br/>(needs DEBUG log)}
    Q3 -- "yes" --> F3[Fix detector exception,<br/>add unit test]:::fix
    Q3 -- "no" --> Q4{task_plans count<br/>for session = 0?}
    Q4 -- "yes" --> F4[Drift fired before<br/>first plan — submit plan<br/>first]:::fix
    Q4 -- "no" --> Q5{LOG_LEVEL = DEBUG?}
    Q5 -- "no" --> F5[severity=debug drifts hide<br/>at INFO; raise log level]:::fix
    Q5 -- "yes" --> F6[Drift kind not on the<br/>scanner's path —<br/>add detector for it]:::fix

    classDef sym fill:#fde2e4,stroke:#c0392b,color:#000
    classDef fix fill:#d4edda,stroke:#27ae60,color:#000
```

## Symptoms

- **Client log**: absence of
  - `WARN harmonograf_client.adk: drift observed kind=<k> severity=<s> detail=<d>`
    (`adk.py:3311` for critical, `adk.py:3313` for info —
    `sev_msg = "drift observed kind=%s severity=%s detail=%s"`).
  - `INFO harmonograf_client.adk: refine: entry hsession=... drift_kind=...`
    (`adk.py:3301`).
- **Server log**: nothing unusual; the server only sees drift
  indirectly via revised plans.
- **UI**: amber pill never appears; drawer plan-revision history is
  static.

## Immediate checks

```bash
# Did any detector scan run?
grep -E 'detect_drift|drift observed|refine: entry' /path/to/agent.log | tail -30

# Did callbacks run at all?
grep -E 'after_model_callback|on_event_callback|before_tool_callback' /path/to/agent.log | tail -20

# Was a tool error visible?
grep -E 'tool_error|on_tool_end|error=' /path/to/agent.log | tail -20
```

## Root cause candidates (ranked)

1. **Detector wasn't on the callback path for this event kind** — e.g.
   the error came from an async tool that the `after_tool_callback`
   didn't observe. If `state.on_tool_end(..., error=...)`
   (`adk.py:1369`) wasn't called, drift detection isn't scheduled.
2. **Detect_drift raised and was swallowed** —
   `DEBUG harmonograf_client.agent: HarmonografAgent: detect_drift raised: <exc>`
   (`agent.py:562`, `agent.py:780`). If you're at INFO level, you
   won't see this.
3. **Callback path never fired** — `after_model_callback` /
   `on_event_callback` is the belt-and-suspenders path; if neither
   runs, no scan happens. Typically because the ADK version skipped
   calling the callback for the event type you care about.
4. **Tool reported success despite erroring** — the tool implementation
   swallowed its exception and returned a value. The adapter has no
   way to know the tool failed. Same symptom as (1).
5. **Drift fired but throttled** — rare, because drift-not-firing
   usually means zero entries in the log, but worth checking:
   `refine: throttled kind=...`.
6. **Plan was None when drift was evaluated** — `refine_plan_on_drift`
   early-exits if there's no current plan; no log, no action. This
   happens when a drift fires before the first plan was submitted.
7. **Drift severity dropped to DEBUG** — info/warning severity is
   logged at INFO or WARNING, but DEBUG-severity drifts are logged at
   DEBUG (`adk.py:3315`). If your log level is INFO, you won't see
   them.

## Diagnostic steps

### 1. Callback coverage

Turn on `LOG_LEVEL=DEBUG` for `harmonograf_client.adk` and
`harmonograf_client.agent`:

```bash
grep -E 'before_tool_callback|after_tool_callback|on_tool_end|on_event_callback' /path/to/agent.log | tail -40
```

You should see every ADK callback cross this log. If only some kinds
appear, that is your gap.

### 2. detect_drift exception

```bash
grep 'detect_drift raised' /path/to/agent.log
```

Fix the underlying exception; it is usually a TypeError against a
dict shape that changed.

### 3. Callback never fired

Grep for the specific ADK event type:

```bash
grep -E 'invocation_id|event_type' /path/to/agent.log
```

If the ADK version doesn't fire `on_event_callback` for
`StateDelta` events, the whole delegated-mode drift scanner is blind.
You need to upgrade ADK, or add a pre-call hook.

### 4. Tool swallowed error

Read the tool's source. If it has `try: ... except: return {...}`,
the adapter never sees the failure. No quick fix — audit the tool.

### 5. Throttled

```bash
grep 'refine: throttled' /path/to/agent.log
```

### 6. Plan was None

```bash
grep 'apply_drift_from_control: no active sessions' /path/to/agent.log
# and more generally:
sqlite3 data/harmonograf.db \
  "SELECT COUNT(*) FROM task_plans WHERE session_id='SESSION_ID';"
```

If zero, the drift fired before a plan existed. Submit a plan first.

### 7. DEBUG severity

```bash
grep -i 'drift observed.*severity=debug' /path/to/agent.log
```

If present, the detector *is* running; it's just quiet at INFO.

## Fixes

1. **Missing callback path**: add the hook for the event kind you care
   about. For tool errors, verify `after_tool_callback` is wired and
   that tools raise exceptions instead of returning error objects.
2. **detect_drift exception**: fix it and add a unit test.
3. **Callback never fired**: upgrade ADK to a version that fires the
   callback, or implement your own wrapper.
4. **Tool swallowed error**: stop swallowing; let exceptions propagate
   so `on_tool_end` sees `error=<exc>`.
5. **Throttled**: not a bug; see
   [`plan-revisions-not-appearing.md`](plan-revisions-not-appearing.md).
6. **Plan missing**: ensure the plan is submitted first; see
   [`plan-never-gets-submitted.md`](plan-never-gets-submitted.md).
7. **Severity = DEBUG**: raise log level or change the detector to
   emit at INFO.

## Prevention

- Audit every tool for swallowed exceptions on import — a tool that
  returns `{"error": "..."}` is invisible to drift detection.
- Unit-test each drift detector against a canned event log from the
  ADK you support.
- In CI, assert that a forced tool exception produces at least one
  `drift observed kind=tool_error` log line.

## Cross-links

- [`dev-guide/debugging.md`](../dev-guide/debugging.md) §"A drift fires
  repeatedly" — symmetric problem.
- [`runbooks/plan-revisions-not-appearing.md`](plan-revisions-not-appearing.md).
- [`runbooks/orchestration-mode-mismatch.md`](orchestration-mode-mismatch.md).
