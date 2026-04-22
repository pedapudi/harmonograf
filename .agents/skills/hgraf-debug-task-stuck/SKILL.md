---
name: hgraf-debug-task-stuck
description: Diagnostic playbook when a task appears stuck. Post-overlay-era: PENDING → NOT_NEEDED at invocation end is expected.
---

# hgraf-debug-task-stuck

## When to use

- A task in the Gantt sits at `PENDING` or `RUNNING` long past
  when you expected it to resolve.
- The intervention timeline shows no drift firing despite the
  obvious stall.
- The agent's `⚠ stuck` stripe is lit in the Graph view header.

Under the overlay model (goldfive#141-144), a task that sits at
`PENDING` throughout a finished run and then flips to
`NOT_NEEDED` at the end is **working as intended**. Only worry if
the agent is actively running and the task still doesn't move.

## Entry points

This skill is a short index. For depth, go to the runbooks:

| Situation | Runbook |
|---|---|
| Task stuck at PENDING while agent runs | [runbooks/task-stuck-in-pending.md](../../../docs/runbooks/task-stuck-in-pending.md) |
| Task stuck at RUNNING | [runbooks/task-stuck-in-running.md](../../../docs/runbooks/task-stuck-in-running.md) |
| Drift should've fired but didn't | [runbooks/drift-not-firing.md](../../../docs/runbooks/drift-not-firing.md) |
| LLM or tool call hung | [runbooks/high-latency-callbacks.md](../../../docs/runbooks/high-latency-callbacks.md) |
| Context window near limit | [runbooks/context-window-exceeded.md](../../../docs/runbooks/context-window-exceeded.md) |

## 30-second triage

1. **Is the run still active?** If not, a PENDING task flipping to
   NOT_NEEDED at invocation end is fine.
2. **Open spans?** SQL:
   ```sql
   SELECT id, kind, name, start_time FROM spans
   WHERE agent_id='<AID>' AND end_time IS NULL
   ORDER BY start_time DESC;
   ```
   A long-open `LLM_CALL` → slow LLM / hung provider.
   A long-open `TOOL_CALL` → tool hung.
   Only an open `INVOCATION` → agent-code loop.
3. **What's `goldfive.llm.duration_ms` on the newest LLM call?**
   Values > 60 000 flag a wedged model.
4. **What do the drift and intervention RPCs say?**
   ```bash
   grpcurl -plaintext -d '{"session_id":"<SID>"}' \
     localhost:7531 harmonograf.v1.Harmonograf/ListInterventions | jq .
   ```
5. **Duplicate plugin?** `grep 'duplicate HarmonografTelemetryPlugin' agent.log`.
   Silent duplicates don't break span emission but DO break the
   `on_cancellation` sweep, leaving RUNNING spans after CANCEL.

## What to send upstream

If you file a bug:

- The session id.
- Output of `ListInterventions`.
- Newest few rows from `task_plans` for the session.
- Goldfive log excerpt containing `DriftDetected` lines.
- py-spy dump of the agent process if it's still running.

## Cross-links

- `dev-guide/debugging.md` for SQL snippets.
- `user-guide/tasks-and-plans.md` for the overlay-era state machine.
