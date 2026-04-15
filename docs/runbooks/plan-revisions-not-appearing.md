# Runbook: Plan revisions not appearing

A refine was triggered (drift, steer, tool error) but the UI still
shows the old plan; the drawer's "Plan revisions" history has no new
entry; the amber "Plan revised" pill never shows.

## Symptoms

- **UI**: current plan stamp reads the previous `revision_index`;
  drawer "Plan revisions" shows N, you expected N+1.
- **Client log**:
  - `INFO harmonograf_client.adk: plan refined: drift=<kind> reason=<text> plan_id=<id>`
    (`adk.py:3400`) — *this is the confirmation line you want to see*.
  - `INFO harmonograf_client.adk: refine: planner.refine returned None (no-op) plan_id=<id>`
    (`adk.py:3419`) — planner chose not to revise.
  - `INFO harmonograf_client.adk: refine: planner.refine returned tasks=<n> plan_id=<id>`
    (`adk.py:3447`) — planner returned a plan.
  - `WARN harmonograf_client.adk: planner.refine raised; ignoring: <exc>`
    (`adk.py:3410`) — planner threw on the refine path.
  - `WARN harmonograf_client.adk: client.submit_plan (drift refine) raised; ignoring: <exc>`
    (`adk.py:3534`) — plan produced but submit failed.
  - `WARN harmonograf_client.adk: client.submit_plan (drift, no refine) raised; ignoring: <exc>`
    (`adk.py:3442`)
  - `INFO harmonograf_client.adk: refine: throttled kind=<k> (last=<x>s ago)`
    (`adk.py:3332`) — drift fired within 2s of a previous one.
  - `INFO harmonograf_client.adk: refine: entry hsession=... drift_kind=... severity=... recoverable=... ...`
    (`adk.py:3301`) — entry trace; you should see this before any of
    the above.

## Immediate checks

```bash
# Did the refine path run at all?
grep -E 'refine: entry|refine: throttled|plan refined|planner.refine' /path/to/agent.log | tail -30

# Did the server receive any new plan revisions?
sqlite3 data/harmonograf.db \
  "SELECT id, revision_index, revision_kind, revision_reason, created_at
   FROM task_plans WHERE session_id='SESSION_ID'
   ORDER BY revision_index DESC LIMIT 5;"

# Did ingest log the new plan?
grep 'task plan received' data/harmonograf-server.log | tail -10
```

## Root cause candidates (ranked)

1. **Refine throttled** — two drifts of the same kind inside
   `_DRIFT_REFINE_THROTTLE_SECONDS = 2.0` (`adk.py:378`) collapse into
   one refine. The second fire is silently swallowed
   (`refine: throttled kind=...`, `adk.py:3332`). Not a bug.
2. **Planner returned no-op** — `refine: planner.refine returned None
   (no-op)` — the planner saw the drift and decided the plan didn't
   need to change. Shows as a revision-reason stamp only, no new tasks.
3. **Planner raised** — `planner.refine raised; ignoring: <exc>`. LLM
   failure, parse failure, or exception in the prompt building.
4. **submit_plan raised** — the new plan existed client-side but never
   made it to the server. Either the transport is down or the plan is
   malformed.
5. **Drift never fired** — you thought a refine was triggered but
   actually nothing detected anything. See
   [`drift-not-firing.md`](drift-not-firing.md).
6. **Server ingested it but the frontend didn't re-render** — the UI
   is subscribed through `TaskRegistry`; if it never got the
   notification, the bug is on the frontend. See
   [`frontend-shows-stale-data.md`](frontend-shows-stale-data.md).
7. **Plan banner dedup** — the banner dedupes on `revisionReason`
   (see `user-guide/troubleshooting.md`); if the reason string is
   identical to the previous revision, the banner doesn't re-flash,
   but the drawer's Plan revisions section *will* have the new entry.

## Diagnostic steps

### 1. Throttled

```bash
grep 'refine: throttled' /path/to/agent.log | tail -10
```

If the throttle is firing repeatedly, the *detector* is firing in a
loop. Investigate why (likely a failing tool on every retry).

### 2. Planner no-op

Look for `refine: planner.refine returned None (no-op)`. That's the
planner's decision; you can't force it to revise. If you disagree,
inspect the planner prompt and tune.

### 3. Planner raised

```bash
grep -A 3 'planner.refine raised' /path/to/agent.log
```

The exception trace is in the next lines. Common: LLM timeout, prompt
too long (context pressure), JSON parse fail.

### 4. submit_plan raised

```bash
grep -E 'client.submit_plan \(drift' /path/to/agent.log | tail -10
```

Then check transport state.

### 5. Drift didn't fire

See [`drift-not-firing.md`](drift-not-firing.md).

### 6. Server-UI gap

```bash
# Did the server actually ingest?
grep 'task plan received' data/harmonograf-server.log | tail -10
```

If yes but the UI is stale, the problem is frontend-side.

### 7. Banner dedup only

Check `revision_reason` in sqlite for the two most recent revisions:

```bash
sqlite3 data/harmonograf.db \
  "SELECT revision_index, revision_kind, revision_reason FROM task_plans
   WHERE session_id='SESSION_ID' ORDER BY revision_index DESC LIMIT 5;"
```

If they're identical, the banner collapsed them; open the drawer to
see the full list.

## Fixes

1. **Throttled**: fix the underlying detector loop; don't disable the
   throttle (it exists to protect the UI).
2. **Planner no-op**: tune the planner prompt, or accept the no-op.
3. **Planner raised**: fix the planner; catch the exception inside
   your planner implementation if it's legitimately optional.
4. **submit_plan raised**: restore transport; the refine is lost.
5. **No drift**: see [`drift-not-firing.md`](drift-not-firing.md).
6. **UI not refreshing**: see
   [`frontend-shows-stale-data.md`](frontend-shows-stale-data.md).
7. **Dedup only**: no fix needed; drawer has the full list.

## Prevention

- Add structured metrics around `refine_fires` and `refine_no_op` so
  you can see them in the stats RPC (`make stats`).
- In your planner implementation, log the pre-refine plan hash and the
  post-refine plan hash — if they're equal, log `no_op` explicitly.

## Cross-links

- [`dev-guide/debugging.md`](../dev-guide/debugging.md) §"The UI shows
  stale data after a refine".
- [`user-guide/troubleshooting.md`](../user-guide/troubleshooting.md)
  §"Drift not firing / plan revision banner not appearing".
- [`runbooks/drift-not-firing.md`](drift-not-firing.md).
