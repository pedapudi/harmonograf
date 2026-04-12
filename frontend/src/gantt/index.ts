// Mutable, non-React data store for the Gantt. React components subscribe for
// presence (agent list, counts) but the hot rendering path reads directly from
// these stores every frame — no setState in the data path.

import type { Agent } from './types';
import { SpanIndex, type DirtyRect } from './spatialIndex';

export type { DirtyRect } from './spatialIndex';
export { SpanIndex } from './spatialIndex';
export type { Agent, Span, SpanKind, SpanStatus, SpanLink, Capability } from './types';

// Registry of agents in a session. Order is join time (stable) — doc 04 §5.1.
export class AgentRegistry {
  private agents: Agent[] = [];
  private byId = new Map<string, Agent>();
  private listeners = new Set<() => void>();

  get list(): readonly Agent[] {
    return this.agents;
  }

  get size(): number {
    return this.agents.length;
  }

  get(id: string): Agent | undefined {
    return this.byId.get(id);
  }

  indexOf(id: string): number {
    const a = this.byId.get(id);
    if (!a) return -1;
    return this.agents.indexOf(a);
  }

  upsert(agent: Agent): void {
    const existing = this.byId.get(agent.id);
    if (existing) {
      Object.assign(existing, agent);
    } else {
      this.byId.set(agent.id, agent);
      this.agents.push(agent);
      this.agents.sort((a, b) => a.connectedAtMs - b.connectedAtMs);
    }
    this.emit();
  }

  setStatus(id: string, status: Agent['status']): void {
    const a = this.byId.get(id);
    if (!a || a.status === status) return;
    a.status = status;
    this.emit();
  }

  setActivityAndStuck(id: string, currentActivity: string, stuck: boolean): void {
    const a = this.byId.get(id);
    if (!a) return;
    a.currentActivity = currentActivity;
    a.stuck = stuck;
    this.emit();
  }

  clear(): void {
    this.agents = [];
    this.byId.clear();
    this.emit();
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(): void {
    for (const fn of this.listeners) fn();
  }
}

// A Session couples an AgentRegistry and a SpanIndex. The renderer takes one.
export class SessionStore {
  readonly agents = new AgentRegistry();
  readonly spans = new SpanIndex();
  // Session start timestamp (wall clock ms). startMs/endMs in spans are
  // session-relative, so this only matters for display formatting.
  wallClockStartMs = 0;

  // Current wall-clock "now" relative to session start. Advanced by the
  // renderer each frame (or by the transport when paused).
  nowMs = 0;

  clear(): void {
    this.agents.clear();
    this.spans.clear();
    this.nowMs = 0;
  }
}

export function emptyDirty(): DirtyRect {
  return { agentId: null, t0: 0, t1: 0 };
}
