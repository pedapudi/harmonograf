# Runbooks

> **Post-goldfive-migration scope.** Orchestration (plan submission, plan
> revisions, drift classification, orchestration modes, state-machine
> invariants) moved to [goldfive](https://github.com/pedapudi/goldfive).
> Runbooks that debugged those codepaths have been removed; the ones that
> remain here cover transport, storage, frontend rendering, and the symptoms
> an operator sees from harmonograf's side. For orchestration debugging,
> start from goldfive's own docs.

Operator-facing diagnostic playbooks. These differ from
[`user-guide/troubleshooting.md`](../user-guide/troubleshooting.md) (which
is for someone looking at the UI and wondering why a button didn't do what
they expected) and from [`dev-guide/debugging.md`](../dev-guide/debugging.md)
(which is for someone who has the source checked out and is writing code).

A runbook here is for an **operator** — probably on-call, probably under
time pressure, probably with a log tail and a sqlite file and a process
list and nothing else. Each runbook maps a concrete symptom (a real log
line, a specific UI banner, an error the user reported) to:

1. **Symptoms** — what you're seeing.
2. **Immediate checks** — one-liners you can paste into a shell.
3. **Root cause candidates** — ranked by how often each one is the answer.
4. **Diagnostic steps** — per candidate, how to confirm or rule it out.
5. **Fixes** — per candidate, how to recover.
6. **Prevention** — how to keep this from happening again.

Every log string quoted below is grep-able against the source. If a
quoted line no longer appears, the code has moved; grep again.

## Catalogue

Ordered roughly by severity × frequency (most-likely first within each
severity band).

### Critical — data loss, agents down, demo wedged

| # | Runbook | When to read it |
|---|---|---|
| 01 | [agent-not-connecting](agent-not-connecting.md) | Session picker empty; agent logs say it tried to stream but got nothing back. |
| 02 | [agent-disconnects-repeatedly](agent-disconnects-repeatedly.md) | Transport bar flickers; `transport disconnected` loops; heartbeats sweep. |
| 12 | [sqlite-errors](sqlite-errors.md) | `database is locked` / `no such table` / corrupt DB after crash. |
| 18 | [post-crash-recovery](post-crash-recovery.md) | Server died mid-run; agents reconnecting; resume behaviour unclear. |
| 16 | [demo-wont-start](demo-wont-start.md) | `make demo` prints something and nothing works. |

### High — tasks stuck, visualisation lying

| # | Runbook | When to read it |
|---|---|---|
| 04 | [task-stuck-in-pending](task-stuck-in-pending.md) | Plan rendered but tasks never enter RUNNING. |
| 05 | [task-stuck-in-running](task-stuck-in-running.md) | Task pinwheels forever; stuck amber border on agent row. |
| 08 | [drift-not-firing](drift-not-firing.md) | Tool errored, agent refused, banner silent. |
| 10 | [span-tree-looks-wrong](span-tree-looks-wrong.md) | Orphans, impossible parents, cross-agent link chaos. |

### Medium — degraded quality

| # | Runbook | When to read it |
|---|---|---|
| 09 | [payloads-missing](payloads-missing.md) | Drawer shows "not preserved" or spinner hangs. |
| 11 | [frontend-shows-stale-data](frontend-shows-stale-data.md) | UI and sqlite disagree. |
| 14 | [high-latency-callbacks](high-latency-callbacks.md) | Agent tail latency grew; ADK callbacks slow. |
| 15 | [context-window-exceeded](context-window-exceeded.md) | Model refuses / truncates; `context_window_tokens` near limit. |
| 19 | [thinking-not-visible](thinking-not-visible.md) | Thinking text exists but UI does not render it. |
| 20 | [minimap-desync](minimap-desync.md) | Minimap viewport drifts from main Gantt. |

## Reading order for a new on-call

If you have never done harmonograf on-call before, read these four
before your first shift and skim the rest:

1. [`agent-not-connecting`](agent-not-connecting.md) — the 3am page.
2. [`task-stuck-in-running`](task-stuck-in-running.md) — the 3am drift.
3. [`sqlite-errors`](sqlite-errors.md) — what a locked DB looks like.
4. [`demo-wont-start`](demo-wont-start.md) — cheap win for triaging
   environmental problems from logic problems.

## Conventions

- Paths are repo-relative (from `/home/sunil/git/harmonograf` or whichever
  checkout you have).
- `$DATA_DIR` defaults to `./data/`. Override with `--data-dir`.
- Log strings quoted here use `log.warning("transport disconnected: %s", e)`
  form — that is the actual source line. At runtime you will see it
  formatted with the substituted value.
- `sqlite3` commands assume the default store filename
  `$DATA_DIR/harmonograf.db`.
- When a runbook says "grep the client log", assume the agent is writing
  to stderr; use `journalctl`, `docker logs`, or whatever ships the
  process stdout in your deployment.

## When nothing fits

- Escalate to [`dev-guide/debugging.md`](../dev-guide/debugging.md) —
  the dev guide has deeper hooks (InvariantChecker repl, live heartbeat
  inspection).
- Capture: (a) the client log since last restart, (b) the server log
  since last restart, (c) a sqlite dump of the offending session:
  ```bash
  sqlite3 data/harmonograf.db ".dump" > /tmp/snapshot.sql
  ```
  and file it with the engineering team.
