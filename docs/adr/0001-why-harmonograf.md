# ADR 0001 — Why Harmonograf exists

## Status

Accepted.

## Context

Distributed tracing solved a problem: microservice calls span many processes, and
you need a tree view to understand any single request. Most "agent observability"
tools today are that same distributed tracer with the word "agent" glued on.
Every LLM call and every tool call becomes a span, the spans nest into a tree,
and a UI renders the tree as a waterfall. For a single agent calling four tools
this is fine.

For a multi-agent system — multiple autonomous LLM-driven processes collaborating
on a plan — the span tree loses almost every piece of information an operator
actually needs:

1. **Plans are first-class and span trees have nowhere to put them.** A real
   agent rollout begins with a plan: a DAG of tasks with dependencies, expected
   assignees, success criteria. The span tree flattens that plan into "whatever
   happened to call whatever" and leaves the operator to reconstruct the intent
   by reading prose payloads in chronological order. When the plan changes
   mid-run — the model re-orders tasks, discovers missing work, hits an error
   and reroutes — the trace records a different tree, not a *change*.

2. **Inferring task state from span lifecycle is wrong.** The intuitive shortcut
   is to say "span closed => task done." This breaks on every real agent:
    * a sub-agent whose span closes may have handed control back while a
      long-running tool call is still in flight;
    * an LLM that writes "task complete" in prose may or may not have actually
      done the work — prose parsing cannot tell "I will complete the task"
      apart from "task complete";
    * concurrent sub-agents in a parallel DAG race through span-close callbacks
      in arbitrary order, producing task-state orderings that contradict the
      true happens-before of the plan.
    Harmonograf's own earlier iterations tried each of these heuristics and
    broke on each of them (see ADR 0011 for the iter15 pivot that killed the
    approach entirely).

3. **Observability is not the job.** An operator who can see a stuck agent but
   cannot unstick it without killing the process has half a tool. Multi-agent
   runs are long, multi-step, and expensive; the cost of discarding a run and
   starting over is an order of magnitude higher than the cost of nudging one.
   A console for multi-agent systems has to let you *intervene* on the same
   live channel you *observe* on.

4. **Framework sandboxes are narrow.** Google ADK (and frameworks like it) run
   agents under strict lifecycle hooks with tight restrictions on how you can
   influence execution from the outside. "Just attach a debugger and patch the
   flow" doesn't work. Coordination has to go through session state, tool
   calls, and event callbacks or it doesn't go through at all.

## Decision

Build harmonograf as a **plan-aware, explicit-state, bidirectional** console
specifically for multi-agent systems. Not a tracer with extras: a different
product with a different data model.

The shape that falls out of the four forcing functions above:

- Plans are canonical objects in the data model, separate from spans and able
  to be *revised* live (see ADR 0013).
- Task state is a monotonic state machine driven by agents calling explicit
  reporting tools, not inferred from span lifecycle (ADR 0011, ADR 0017).
- The wire protocol is bidirectional so the UI can send control events back to
  agents (ADR 0004, ADR 0005).
- Integration with ADK goes through official seams only — callbacks, session
  state, tool injection — never monkey-patching (ADR 0003, ADR 0014).

## Consequences

**Good.**
- The UI can answer "what is the agent *supposed* to be doing right now, and is
  it doing it?" — a question span-only tools cannot answer at all.
- Bidirectional control means an operator nudging a stuck run is a primary
  flow, not a bolt-on.
- The data model doesn't lie about task completion; terminal states are
  declared, not inferred.

**Bad.**
- Agents have to cooperate — an uninstrumented third-party agent contributes
  spans but not task state. This is an acceptable floor for ADK-first v0 but
  limits the "drop in and observe" story that pure tracers enjoy.
- Harmonograf is opinionated about the execution model (plan → tasks → reports).
  Agents that don't plan up front (open-ended ReAct loops without a task DAG)
  have to be bent into the model or give up the plan-diff view.
- Shipping both observability *and* coordination widens the product surface.
  Feature work is on roughly twice as many fronts as a pure observability tool.
