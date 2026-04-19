# Runbook: Thinking not visible

> **Post-goldfive note.** The `llm.thought` aggregate referenced below
> is now emitted by goldfive's `ADKAdapter`, not the old
> `HarmonografAgent`. Harmonograf still stamps `thinking_text` /
> `thinking_preview` from its telemetry plugin. The UI selection order
> is unchanged; log lines about `record_llm_thought` / `agent.py` come
> from goldfive now.

The agent's LLM is emitting reasoning ("thinking") content but the UI
isn't rendering it. Either the bars are missing from the Gantt's
thinking track, or the drawer's Thinking tab is empty.

**Triage decision tree** — split producer (model emits → adapter captures →
attribute stamped) from consumer (frontend reads the right key).

```mermaid
flowchart TD
    Start([Thinking absent in UI]):::sym --> Q1{Model exposes<br/>reasoning channel?}
    Q1 -- "no" --> F1[Model doesn't surface<br/>thinking — switch model<br/>or accept]:::fix
    Q1 -- "yes" --> Q2{Log: emit_thinking_<br/>as_task_report?}
    Q2 -- "no" --> Q2a{record_llm_thought<br/>failed in DEBUG log?}
    Q2a -- "yes" --> F2a[Fix exception in<br/>record_llm_thought]:::fix
    Q2a -- "no" --> F2b[capture_thinking opt-in<br/>not enabled in config]:::fix
    Q2 -- "yes" --> Q3{Span attributes carry<br/>llm.thought / task_report?}
    Q3 -- "no" --> F3[emit_span_update raised:<br/>fix attribute stamp]:::fix
    Q3 -- "yes" --> Q4{Frontend reads same key<br/>(grep frontend/src)?}
    Q4 -- "no" --> F4[Producer/consumer key drift —<br/>canonicalise the name]:::fix
    Q4 -- "yes" --> F5[Routing mismatch task_report<br/>vs llm.thought; pick one]:::fix

    classDef sym fill:#fde2e4,stroke:#c0392b,color:#000
    classDef fix fill:#d4edda,stroke:#27ae60,color:#000
```

## Symptoms

- **UI**: Gantt shows LLM_CALL spans but no accompanying thinking
  stripes; drawer Thinking tab is empty or shows "no thought
  captured".
- **Client log**:
  - `INFO harmonograf_client.adk: emit_thinking_as_task_report inv=<id> report=<repr>`
    (`adk.py:4923`) — this is the canonical emit path.
  - Absence of the above means the emit path never ran.
  - `DEBUG harmonograf_client.agent: HarmonografAgent: record_llm_thought failed: <exc>`
    (`agent.py:1829`).
  - `DEBUG harmonograf_client.agent: HarmonografAgent: emit_span_update(llm.thought) failed: <exc>`
    (`agent.py:1838`).
- **sqlite**: no rows in `spans` with a `llm.thought` attribute or
  similar.

## Immediate checks

```bash
# Emit path ran?
grep 'emit_thinking_as_task_report\|record_llm_thought\|llm.thought' /path/to/agent.log | tail -30

# Any thinking attributes in spans?
sqlite3 data/harmonograf.db \
  "SELECT id, kind, name FROM spans WHERE session_id='SESSION_ID'
   AND json_extract(attributes, '$.\"llm.thought\"') IS NOT NULL LIMIT 10;"

# Or task_report attributes (thinking often routes through this):
sqlite3 data/harmonograf.db \
  "SELECT id, kind FROM spans WHERE session_id='SESSION_ID'
   AND json_extract(attributes, '$.task_report') IS NOT NULL LIMIT 10;"
```

## Root cause candidates (ranked)

1. **Model doesn't emit thinking** — not every provider surfaces
   reasoning content as a separate channel. If the LLM response is
   just `text`, there's nothing to capture. This is the most common
   case.
2. **record_llm_thought raised and was swallowed** — see
   `agent.py:1829`. The event existed but the handler threw. At
   INFO level you'd see nothing.
3. **emit_span_update(llm.thought) raised** — `agent.py:1838`. Same
   story, different step.
4. **Thinking extracted but never stamped as an attribute** — the
   agent captured the text but didn't attach it to any span. The
   drawer has no span to bind to.
5. **Frontend doesn't render this attribute** — the data is in
   sqlite but the Gantt / drawer doesn't read the right key. Check
   `frontend/src/components/shell/views/` for the thinking view
   code.
6. **Thinking channel disabled in agent config** — an explicit
   opt-in may be required (`capture_thinking=True` or equivalent).
7. **Thinking routed to `task_report` but UI expects `llm.thought`**
   — the agent routes thinking through `task_report` attribute
   (`ingest.py:352-355`), but the drawer looks for `llm.thought`.
   Mismatch between producer and consumer.

## Diagnostic steps

### 1. Model emits thinking at all

Dig into the raw LLM response in an ad-hoc call. If the response
schema has a `reasoning` / `thinking` / `thoughts` field, great;
if not, the model simply doesn't provide it.

### 2. record_llm_thought raised

Turn on DEBUG for `harmonograf_client.agent` and grep:

```bash
grep 'record_llm_thought failed' /path/to/agent.log
```

The exception message identifies the cause.

### 3. emit_span_update raised

```bash
grep 'emit_span_update(llm.thought) failed' /path/to/agent.log
```

### 4. Attribute never stamped

Check sqlite:

```bash
sqlite3 data/harmonograf.db \
  "SELECT attributes FROM spans WHERE id='SPAN_ID';"
```

If `attributes` has no thinking key, the stamp never happened.

### 5. Frontend rendering

```bash
grep -rn 'llm.thought\|thought\|thinking' frontend/src/
```

If the frontend key doesn't match what the backend writes, you
found it.

### 6. Config opt-in

```bash
grep -rn 'capture_thinking\|thinking_mode' /path/to/agent_src/
```

### 7. Routing mismatch

Compare what the client writes
(`agent.py` around `record_llm_thought`) with what the drawer
reads. These should be one canonical attribute key — if they've
drifted, fix one side.

## Fixes

1. **Model doesn't emit**: switch to a model that exposes reasoning,
   or accept that this session type has no thinking track.
2. **record_llm_thought raised**: fix the exception — usually a
   TypeError against an unexpected content shape.
3. **emit_span_update raised**: same.
4. **Attribute not stamped**: ensure the code path that captures
   thinking actually calls `emit_span_update(attributes={"llm.thought": ...})`.
5. **Frontend key mismatch**: align the key. Pick one canonical
   name.
6. **Config disabled**: enable `capture_thinking=True` or
   equivalent in agent construction.
7. **Routing mismatch**: fix the producer or the consumer so they
   agree on whether thinking rides on `task_report` or `llm.thought`.

## Prevention

- Canonicalise the thinking attribute name in one place and
  import it from both client and frontend.
- Add a capture-test in CI: emit a canned thinking payload, assert
  the attribute lands in sqlite and the frontend renders it.
- Don't swallow exceptions in the capture path; at least bump a
  metric so silent loss is observable.

## Cross-links

- Task #4 in [`milestones.md`](../milestones.md) — the integration
  work for thinking visualisation (completed).
- [`dev-guide/client-library.md`](../dev-guide/client-library.md)
  for the emit/record attribute contract.
- [`runbooks/span-tree-looks-wrong.md`](span-tree-looks-wrong.md) if
  the thinking spans exist but aren't parented correctly.
