import type { Task, TaskPlan } from './types';

// Compute execution stages from a TaskPlan's dependency DAG.
//
// stageOf(t) = max(stageOf(dep) for dep in incomingEdges(t)) + 1, with
// stageOf = 0 for tasks with no incoming edges. Tasks within the same stage
// are parallelizable; stages are strictly sequential.
//
// If a cycle is detected, the tasks involved (and any still-unresolved tasks)
// are collapsed into stage 0 and a console.warn is logged — callers still get
// a usable layering.
export function computeStages(plan: TaskPlan): Task[][] {
  const tasks = plan.tasks;
  if (tasks.length === 0) return [];

  const byId = new Map<string, Task>();
  for (const t of tasks) byId.set(t.id, t);

  const incoming = new Map<string, string[]>();
  const outgoing = new Map<string, string[]>();
  for (const t of tasks) {
    incoming.set(t.id, []);
    outgoing.set(t.id, []);
  }
  for (const e of plan.edges) {
    if (!byId.has(e.fromTaskId) || !byId.has(e.toTaskId)) continue;
    incoming.get(e.toTaskId)!.push(e.fromTaskId);
    outgoing.get(e.fromTaskId)!.push(e.toTaskId);
  }

  // Kahn-style longest-path layering.
  const stageOf = new Map<string, number>();
  const indeg = new Map<string, number>();
  for (const t of tasks) indeg.set(t.id, incoming.get(t.id)!.length);

  const queue: string[] = [];
  for (const t of tasks) {
    if (indeg.get(t.id) === 0) {
      stageOf.set(t.id, 0);
      queue.push(t.id);
    }
  }

  let head = 0;
  while (head < queue.length) {
    const id = queue[head++];
    const s = stageOf.get(id)!;
    for (const next of outgoing.get(id)!) {
      const prev = stageOf.get(next);
      const candidate = s + 1;
      if (prev === undefined || candidate > prev) {
        stageOf.set(next, candidate);
      }
      const d = indeg.get(next)! - 1;
      indeg.set(next, d);
      if (d === 0) queue.push(next);
    }
  }

  // Any task not yet assigned a stage is part of (or downstream of) a cycle.
  const unresolved: Task[] = [];
  for (const t of tasks) {
    if (!stageOf.has(t.id)) unresolved.push(t);
  }
  if (unresolved.length > 0) {
    console.warn(
      `computeStages: cycle detected in plan ${plan.id}; ${unresolved.length} task(s) collapsed into stage 0`,
    );
    for (const t of unresolved) stageOf.set(t.id, 0);
  }

  let maxStage = 0;
  for (const s of stageOf.values()) if (s > maxStage) maxStage = s;

  const stages: Task[][] = Array.from({ length: maxStage + 1 }, () => []);
  for (const t of tasks) {
    stages[stageOf.get(t.id)!].push(t);
  }
  return stages;
}
