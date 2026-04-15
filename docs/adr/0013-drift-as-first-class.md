# ADR 0013 — Drift is a first-class event

## Status

Accepted.

## Context

A plan emitted at the start of a run is a prediction. Real agent runs
contradict their plans constantly:

- a tool errors and the agent needs to try a different approach;
- the model refuses to do a task;
- a sub-task is discovered that was not in the original plan;
- a task turns out to be assigned to the wrong agent;
- the user steers mid-run;
- context window pressure forces consolidation;
- the agent decides the entire plan is stale.

Each of these is a moment when the UI must show the operator "the plan you
are looking at is no longer what the system is doing." If harmonograf
represents the initial plan as immutable and lets execution diverge
silently, the operator is reading fiction. If harmonograf throws away the
initial plan whenever anything changes, they lose the ability to see
*what* changed — which is exactly the information they need to decide
whether to intervene.

Span-based observability has no concept of "the plan has been revised."
Traces are append-only; they show what happened but cannot show that
what happened was a substitution for something that was supposed to
happen.

## Decision

Drift is a **first-class event type** with a taxonomy of kinds, and
revisions to the plan are called **refines**. Both live in the wire
protocol and in the state model.

Mechanics:

- The client library emits **deferential refines**: a structured call
  back into the planner with the current plan and the drift context.
  The planner returns a revised plan, which is submitted to the server
  as a new `TaskPlan` message with `revision_reason`, `revision_kind`,
  `revision_severity`, and monotonically incrementing `revision_index`
  (see `proto/harmonograf/v1/types.proto`'s `TaskPlan`).
- `TaskRegistry` on the server upserts the revised plan and computes a
  **plan diff** against the prior revision: added, removed, reordered,
  and re-parented tasks. The diff is computed in the frontend by
  `frontend/src/gantt/index.ts :: computePlanDiff`.
- The frontend renders a **banner** ("plan revised: new work discovered
  by agent X, 2 tasks added") and a **drawer** with a side-by-side diff
  so the operator can see what changed.

Drift kinds (from AGENTS.md's Plan execution protocol section and the
planner module): `tool_error`, `agent_refusal`, `context_pressure`,
`new_work_discovered`, `plan_divergence`, `user_steer`, `user_cancel`,
`task_failed_recoverable`, `task_failed_fatal`, and roughly fifteen more.
Each has a defined refine behavior.

Drift fires from several sources: explicit reporting tools
(`report_new_work_discovered`, `report_plan_divergence`), ADK error
callbacks (`on_tool_error_callback`), and event observer pattern matches
on model output. In every case the result is the same: call planner
refine, submit new `TaskPlan`, let the frontend diff and render.

**Drift → refine → diff pipeline** — multiple drift sources funnel into one
deferential refine call; the resulting `TaskPlan` revision is diffed by the
frontend and rendered as a banner + side-by-side drawer.

```mermaid
flowchart LR
    subgraph Sources["Drift sources"]
      direction TB
      D1[report_new_work_discovered]
      D2[report_plan_divergence]
      D3[on_tool_error_callback]
      D4[event observer prose match]
      D5[user_steer / user_cancel]
      D6[context_pressure heartbeat]
    end
    Sources --> Detect[detect_drift<br/>kind + severity]
    Detect --> Refine[planner.refine<br/>(LLM call)]
    Refine --> Submit[submit_plan<br/>revision_index++]
    Submit --> Reg[server TaskRegistry upsert]
    Reg --> Diff[computePlanDiff<br/>frontend/src/gantt/index.ts]
    Diff --> UI[Banner + drawer side-by-side]

    classDef good fill:#d4edda,stroke:#27ae60,color:#000
    class Refine,Submit,Diff,UI good
```

## Consequences

**Good.**
- The operator sees plan changes as *changes*, not as "a different tree."
  The banner and drawer make it visually obvious when the system has
  replanned and why.
- Drift kinds are a stable taxonomy. A future analytics pass can count
  "how often did agent X hit `tool_error` on task Y" — the data is
  already tagged.
- The refine path is single-sourced. All drift leads to the same
  planner-refine RPC and the same diff machinery, regardless of which
  callback detected the drift. One code path, many entry points.
- Plans are revisioned, not mutated. `revision_index` is monotonic;
  the old revisions stay in the store so playback works.

**Bad.**
- **Refine calls are LLM calls.** Every refine burns tokens, and a
  chatty drift source (a flaky tool that errors on every invocation)
  can produce a refine storm. We mitigate with deferential cooldowns
  inside the planner, but the cost is real.
- **The drift taxonomy is long.** Every new kind is a proto enum value
  (the severity strings are free-form for now — see `revision_severity`)
  plus documentation in `reporting-tools.md` plus a branch in the
  planner. Adding kinds has a non-trivial per-kind cost.
- **Plan diffs are approximate.** `computePlanDiff` matches tasks
  across revisions by id where it can and by heuristic where it cannot
  (a renamed task produces a remove + add pair rather than a rename
  edge). The diff is useful but not always semantically minimal.
- **Storage grows.** Each revision is a full `TaskPlan`, not a delta.
  A chatty session with many refines accumulates plan revisions
  proportional to refine count.
- **Drift detection is heuristic.** Not every drift signal is reliable —
  prose-based kinds (model says "I think the plan is wrong") have the
  same ambiguity problems that killed span-lifecycle inference (ADR
  0011). We treat prose signals as secondary and prefer explicit
  reporting-tool drift.

Drift as a first-class event is what distinguishes harmonograf's plan
view from "here is the original plan, good luck." It turns replanning
from a failure mode into a product surface.
