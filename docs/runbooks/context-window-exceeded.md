# Runbook: Context window exceeded

The agent's model is hitting its context window limit. LLM calls
start failing, get truncated, or are refused outright.

## Symptoms

- **UI**: drawer's context-window visualization shows
  `tokens / limit_tokens` at or near 1.0; Gantt marks recent LLM_CALL
  spans with errors.
- **Client log**:
  - `drift observed kind=context_pressure severity=... detail=...`
    — drift detector fired the `context_pressure` drift kind.
  - Possibly `plan refined: drift=context_pressure reason=... plan_id=...`
    if the refine took effect.
  - LLM client library errors, e.g. `google.api_core.exceptions.InvalidArgument:
    prompt too long` or provider-specific equivalents.
- **Heartbeat**: `context_window_tokens` ≥ `context_window_limit_tokens`
  — see `ingest.py:587-609` for the sample publish path.

## Immediate checks

```bash
# What's the most recent context-window sample on the bus/store?
sqlite3 data/harmonograf.db \
  "SELECT recorded_at, tokens, limit_tokens FROM context_window_samples
   WHERE session_id='SESSION_ID' AND agent_id='AGENT_ID'
   ORDER BY recorded_at DESC LIMIT 10;"

# Drifts fired with context_pressure kind?
grep 'drift observed kind=context_pressure' /path/to/agent.log | tail -10

# Most recent LLM_CALL status:
sqlite3 data/harmonograf.db \
  "SELECT id, status, name, end_time FROM spans
   WHERE session_id='SESSION_ID' AND kind='LLM_CALL'
   ORDER BY start_time DESC LIMIT 5;"
```

## Root cause candidates (ranked)

1. **Genuine long session** — the agent has been running for a long
   time and has accumulated history beyond what the model can hold.
   Most common; not a bug, just a limit.
2. **Big system prompt** — the template or retrieved-context blob is
   huge relative to the window. Common when retrieval is returning
   too many chunks.
3. **Past task results baked into state** — `session.state` holds
   `harmonograf.completed_task_results`; if the agent re-injects
   every result into every prompt, it grows unbounded.
4. **Attachments / tool outputs in history** — big tool outputs
   (webpages, file contents) inflate the context turn after turn.
5. **Wrong model limit configured** — the client library has the
   model's context window wrong (configured for 128k but the model
   is 32k), so the limit-check undercounts and you overflow in
   reality.
6. **No trimming / summarisation** — the agent never prunes history;
   every turn adds more.
7. **Tokenizer mismatch** — counting tokens with a tokenizer that
   under-reports. You think you have headroom; the real model is
   full.

## Diagnostic steps

### 1. Long session

Look at the spans timeline for the agent; if it's been running for
hours and the context has been climbing monotonically, this is
simply expected.

### 2. System prompt size

Capture a single LLM_CALL payload and count characters:

```bash
sqlite3 data/harmonograf.db \
  "SELECT summary FROM payloads WHERE digest=(
     SELECT payload_digest FROM spans WHERE id='SPAN_ID');"
```

Estimate tokens as `chars / 3.5` (rough).

### 3. State bloat

Dump `session.state` in a repl attached to the agent:

```python
print({k: len(str(v)) for k, v in session.state.items()})
```

Look for the biggest key.

### 4. Tool outputs

Same as (2) but on the tool output spans.

### 5. Limit misconfigured

Grep for the model's configured limit in your agent code:

```bash
grep -rn 'context_window_limit\|context_window_tokens' /path/to/agent_src/
```

Compare to provider docs.

### 6. No trimming

Check whether your agent's state has a pruner / summariser hook. If
not, add one.

### 7. Tokenizer mismatch

Compare two samples: tokenize the same prompt with the provider's
tokenizer and with yours. If they differ by >10%, swap to the
provider's.

## Fixes

1. **Long session**: split into multiple sub-sessions; use steer to
   restart with a summary.
2. **Big system prompt**: compress, filter, or chunk the retrieval.
3. **State bloat**: stop injecting all completed-task results; only
   include the ones relevant to the current task.
4. **Tool outputs**: truncate tool outputs at emit time; reference by
   URL / digest where possible.
5. **Limit misconfigured**: correct the constant so the limit-check
   reflects reality.
6. **Trimming**: implement a simple head/tail prune or an LLM-based
   summariser.
7. **Tokenizer**: switch to the provider's tokenizer.

## Prevention

- Alert on `tokens / limit_tokens > 0.85` — you want to catch this
  before the refusal.
- Always use the provider's own tokenizer when possible.
- Budget history: pick a max history length per agent and enforce it.

## Cross-links

- [`dev-guide/debugging.md`](../dev-guide/debugging.md) §"An agent is
  stuck" — context pressure often shows up as stuckness first.
- Docs on context-window visualization (task #3 / task #2 milestones)
  for how the UI renders this state.
- `server/harmonograf_server/ingest.py:587-609` — context-window
  telemetry ingest path.
