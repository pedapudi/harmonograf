---
name: hgraf-spawn-agent-team
description: Organize iter-style meta-work across a multi-agent Claude Code team — TeamCreate, task-claim flow, coordination via SendMessage, completion reporting.
---

# hgraf-spawn-agent-team

## When to use

You are a team-lead agent (or the human operator acting as one) who wants to decompose a large harmonograf iteration into parallel tasks executed by multiple Claude Code sub-agents. This is how iter15 and iter16 batches have been run — one lead spawns N teammates, each claims a task from a shared queue, coordinates by message, and reports completion.

This skill is specifically about the **meta-work organization**, not about writing code. For writing code, use the relevant task-specific skills.

## Prerequisites

1. Know the difference between a **subagent** (spawned via the `Agent` tool — ephemeral, returns a single result) and a **teammate** (spawned via `TeamCreate` — persistent, addressable by name, communicates via `SendMessage`).
2. Understand the tool surface:
   - `TeamCreate` — spin up a new team with named members.
   - `TaskCreate` / `TaskList` / `TaskGet` / `TaskUpdate` — shared task queue visible to all teammates.
   - `SendMessage` — addressed or broadcast messages between teammates.
   - `Monitor` — stream stdout from a long-running subagent's output.
3. Read the conversation state: the team-lead typically receives an opening message from the human describing the iteration goal, a list of topics, and hard constraints (where files live, naming conventions, etc.).

## Team-lead responsibilities

### 1. Decompose the work into discrete tasks

Each task should be:

- **Self-contained** — a teammate should be able to execute it with only the task description plus access to the repo. No implicit dependencies on another teammate's in-progress state.
- **Sized for one session** — roughly 30 min to 2 hours of model time. Larger tasks go stale; smaller tasks have too much orchestration overhead.
- **Independently verifiable** — the task has a clear "done" condition that the lead can check without reading every line of output.

Good decomposition: "write 20 skills, each in its own file". Bad: "build the Gantt chart".

### 2. Write the task descriptions

Each `TaskCreate` call needs:

- **Title** — one line, imperative mood.
- **Body** — goal, constraints, verification. Be specific about file paths and naming.
- **Acceptance criteria** — how a reviewer (lead or human) judges completion.

Avoid putting instructions in the body that rely on conversation history the teammate won't have. Teammates start cold.

### 3. Spawn the team

`TeamCreate` with:

- A meaningful team name (e.g., `harmonograf-iter16`) so teammates can address each other.
- One member per parallel unit of work. More members than tasks is fine — the extras go idle. Fewer means serialization.
- A `team_prompt` (or equivalent) that establishes shared conventions: where files live, naming, communication norms, completion reporting.

### 4. Broadcast the rules

The first thing each teammate should hear is the ground rules. A broadcast via `SendMessage to: "*"`:

- Where work lives on disk (exact directory, absolute path).
- What files they must NOT touch.
- How to claim a task (`TaskUpdate state=in_progress assignee=<their name>`).
- How to mark done (`TaskUpdate state=completed`) and report to the lead.
- Coordination protocol: when to ask the lead before acting, when to proceed independently.

### 5. Watch the queue

`TaskList` periodically to see claimed/in-progress/completed counts. If a teammate claims a task and then goes silent, `Monitor` their output or ping them.

### 6. Coordinate overlaps

If two teammates are working on related files, warn them both. Use `SendMessage` to introduce them and ask them to share file:line ownership. Don't let them discover the conflict via a merge.

### 7. Review completions

When a teammate marks a task complete:

- Check the actual artifacts (read files, run tests).
- Trust but verify — summaries describe intent, not necessarily outcome.
- If the work is incomplete, reopen the task with specific corrective guidance.
- If complete, thank the teammate and (optionally) give them a follow-up.

### 8. Report to the human

When the whole iteration completes, write a single summary to the human: tasks done, notable decisions, loose ends. Include paths to the artifacts so the human can inspect.

## Teammate responsibilities

### 1. Claim before working

`TaskUpdate state=in_progress assignee=<self>` atomically. Two teammates reaching for the same task is the lead's problem to prevent via ordering, but defensive claiming is cheap.

### 2. Read the ground rules before the task body

The team broadcast has the critical constraints (path, naming, do-not-touch list). Violating those wastes the lead's time later.

### 3. Acknowledge hard constraints back to the lead

If the task description has an ambiguous or surprising constraint ("write to path X, NOT path Y"), reply to the lead confirming you understood. Better to trade one round trip than produce work in the wrong location.

### 4. Coordinate with concurrent teammates

If your task overlaps with another teammate's, send them a message directly. Don't route everything through the lead — peer-to-peer coordination is faster and the lead has limited context budget.

### 5. Ground the work in real code

Read before writing. Grep before claiming a symbol exists. Don't invent file:line references. If the task says "reference `foo.py:123`" and line 123 is blank, flag it to the lead rather than making up a plausible location.

### 6. Mark complete + report

When done: `TaskUpdate state=completed`, then `SendMessage to=<lead> body=<summary>`. The summary should include: what was produced, where, any surprises, what verification was run.

## Protocol for iteration-style work (iter15, iter16, …)

Harmonograf uses "iter" tags to bundle related work. An iter typically has:

- A **lead** task (e.g., `#1` — "iter16 lead: coordinate batch X").
- A set of **worker** tasks (e.g., `#10` through `#20` — one per topic).
- A **follow-up** task for integration (e.g., `#30` — "review and merge iter16 output").

Task IDs are stable. Teammates reference them in messages (`"claimed #16, starting now"`). The lead uses `TaskList` to see the whole queue.

Naming convention: when writing files produced by an iter, prefix them so future iters don't stomp. Example: `.agents/hgraf-<topic>.md` — the `hgraf-` prefix is the repo's convention; the iter number is implicit in git history.

## Step-by-step recipe: starting a new iter

### 1. Decide the iter goal

One sentence: "Expand skills library — batch 2, topics A–T." The goal dictates decomposition.

### 2. Enumerate the topics

Before spawning anyone, write out the full list. If you can't list it, you can't delegate it. Verify the topics don't overlap and aren't already covered by a prior iter.

### 3. Create the tasks in the queue

`TaskCreate` one task per topic. Assign an iter-level tag or prefix so `TaskList` can filter them later.

### 4. Spawn the team with the right headcount

Rule of thumb: spawn `min(N_topics, 4)` teammates. More than 4 rarely helps — lead bandwidth for reviewing becomes the bottleneck.

### 5. Broadcast the ground rules

As described above.

### 6. Wait and respond

Use `Monitor` or passive waiting via the message inbox. When a teammate asks a question, answer it. When a teammate completes a task, review it. When a teammate claims one, note it.

### 7. Handle the long tail

The last one or two tasks often take disproportionately long (hardest topics, or tired teammates). Consider reassigning to a fresh teammate if one is stuck.

### 8. Close out

When the queue is empty and all tasks complete, do a final review sweep, then report to the human.

## Common pitfalls

- **Starting teammates without ground rules**: they write files to the wrong location. Always broadcast constraints before anyone starts work.
- **Putting context in the task body that the teammate can't see**: tasks are read cold. "As we discussed earlier, do X" is invisible to the teammate.
- **Letting two teammates silently overlap**: the lead sees both `in_progress`, doesn't match them up, and merges two conflicting diffs. Name overlaps explicitly.
- **Reviewing too late**: if you wait until every teammate is done, you amplify any systematic mistake across N outputs. Review the first one or two completions early to catch pattern errors.
- **Trusting summaries without checking files**: a teammate's "I wrote 10 skills" might mean 10 files or 3. Run `ls` and `wc -l`.
- **Flooding the team channel**: broadcasts interrupt everyone. Prefer targeted messages. Reserve broadcast for actual rule changes.
- **Spawning a team for work that's one coherent thread**: teams add coordination overhead. A single-agent task queue (or just you) is faster for work that can't be parallelized cleanly.
- **Forgetting to mark tasks completed**: `TaskList` still shows them `in_progress`, the lead thinks work is pending, and the iter feels stalled. Closing the task is part of the work.
- **Conflating `Agent` and teammates**: `Agent` spawns one-shot subagents (great for research). `TeamCreate` spawns persistent addressable members (great for parallel work). Using the wrong one breaks coordination.
- **Ignoring memory**: the team-lead's memory carries iter history. Before starting iter17, read prior iter summaries so you don't repeat decomposition mistakes.
