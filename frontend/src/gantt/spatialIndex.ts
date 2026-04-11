import type { Span } from './types';

// Per-agent interval storage with a bucketed lookup grid. We skipped a full
// interval tree because our workload has two properties that make bucketing
// faster in practice:
//   1. Spans append in time order (within one agent) 99% of the time — the
//      bucket writes are O(1) amortized, no rebalancing.
//   2. Queries are always a contiguous [t0, t1] viewport, not point stabs.
//
// Grid size is chosen per-agent at 64ms — small enough to cull tightly at
// 30-second zoom, large enough that 6-hour sessions only spend ~340k buckets.

const BUCKET_MS = 64;

interface AgentBuckets {
  // Sorted by startMs. Each entry is an index in the flat spans array below.
  spans: Span[];
  // Map from bucketIndex to a range [lo, hi) into `spans` — used to limit scans.
  // We only record the max endMs seen up to each bucket for cheap early-out on
  // overlap queries.
  maxEndPrefix: number[];
  minStartMs: number;
}

export class SpanIndex {
  private agents = new Map<string, AgentBuckets>();
  private byId = new Map<string, Span>();
  private listeners = new Set<(dirty: DirtyRect) => void>();
  private globalMaxEndMs = 0;

  get size(): number {
    return this.byId.size;
  }

  agentIds(): string[] {
    return [...this.agents.keys()];
  }

  maxEndMs(): number {
    return this.globalMaxEndMs;
  }

  subscribe(fn: (dirty: DirtyRect) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(dirty: DirtyRect): void {
    for (const fn of this.listeners) fn(dirty);
  }

  get(id: string): Span | undefined {
    return this.byId.get(id);
  }

  append(span: Span): void {
    // Idempotent on span id: WatchSession can deliver the same span via both
    // the initial snapshot and a live-stream delta if the delta lands in the
    // bus between subscribe and snapshot build. Without dedup, the span would
    // be pushed into `b.spans` twice while `byId` held one entry.
    if (this.byId.has(span.id)) {
      this.update(span);
      return;
    }
    this.byId.set(span.id, span);
    let b = this.agents.get(span.agentId);
    if (!b) {
      b = { spans: [], maxEndPrefix: [], minStartMs: span.startMs };
      this.agents.set(span.agentId, b);
    }
    // Fast path: appending past the end.
    const last = b.spans[b.spans.length - 1];
    if (!last || span.startMs >= last.startMs) {
      b.spans.push(span);
    } else {
      // Slow path: out-of-order insert. Binary search.
      let lo = 0;
      let hi = b.spans.length;
      while (lo < hi) {
        const mid = (lo + hi) >>> 1;
        if (b.spans[mid].startMs <= span.startMs) lo = mid + 1;
        else hi = mid;
      }
      b.spans.splice(lo, 0, span);
    }
    const endMs = span.endMs ?? span.startMs;
    if (endMs > this.globalMaxEndMs) this.globalMaxEndMs = endMs;
    b.maxEndPrefix.length = 0; // invalidated; rebuilt lazily in queryRange
    this.emit({ agentId: span.agentId, t0: span.startMs, t1: endMs });
  }

  update(span: Span): void {
    const existing = this.byId.get(span.id);
    if (!existing) {
      this.append(span);
      return;
    }
    // Same position in buckets; we mutate fields in place so the spans array
    // stays sorted. Time shifts would need a remove+append; skipped for v0.
    existing.status = span.status;
    existing.endMs = span.endMs;
    existing.name = span.name;
    existing.links = span.links;
    existing.replaced = span.replaced;
    existing.payloadRefs = span.payloadRefs;
    existing.attributes = span.attributes;
    existing.error = span.error;
    const endMs = existing.endMs ?? existing.startMs;
    if (endMs > this.globalMaxEndMs) this.globalMaxEndMs = endMs;
    const b = this.agents.get(existing.agentId);
    if (b) b.maxEndPrefix.length = 0;
    this.emit({ agentId: existing.agentId, t0: existing.startMs, t1: endMs });
  }

  remove(id: string): void {
    const existing = this.byId.get(id);
    if (!existing) return;
    const b = this.agents.get(existing.agentId);
    if (!b) return;
    const idx = b.spans.findIndex((s) => s.id === id);
    if (idx >= 0) b.spans.splice(idx, 1);
    this.byId.delete(id);
    b.maxEndPrefix.length = 0;
    this.emit({
      agentId: existing.agentId,
      t0: existing.startMs,
      t1: existing.endMs ?? existing.startMs,
    });
  }

  // Query a half-open interval [t0, t1) for spans on one agent. Running spans
  // (endMs null) are treated as extending to +∞ so the "now" cursor always sees
  // them.
  queryAgent(agentId: string, t0: number, t1: number, out: Span[] = []): Span[] {
    const b = this.agents.get(agentId);
    if (!b || b.spans.length === 0) return out;
    // Binary search for first span with startMs >= t0. Spans that start before
    // t0 may still overlap if their end is past t0 — we scan back from there.
    const spans = b.spans;
    let lo = 0;
    let hi = spans.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (spans[mid].startMs < t0) lo = mid + 1;
      else hi = mid;
    }
    // Walk backwards for straddlers, bounded by a small budget — avoids the
    // pathological case of one giant span covering the whole session. Start
    // strictly before `lo`; the forward walk below covers spans[lo] itself,
    // and clamping to 0 here would double-count the first span.
    let i = lo - 1;
    while (i >= 0) {
      const s = spans[i];
      const end = s.endMs ?? Number.POSITIVE_INFINITY;
      if (end >= t0 && s.startMs < t1) out.push(s);
      if (s.startMs < t0 - BUCKET_MS * 1024) break;
      i--;
    }
    // Walk forward while startMs < t1.
    for (let j = lo; j < spans.length; j++) {
      const s = spans[j];
      if (s.startMs >= t1) break;
      out.push(s);
    }
    return out;
  }

  queryRange(t0: number, t1: number, agentFilter?: Set<string>): Span[] {
    const out: Span[] = [];
    for (const agentId of this.agents.keys()) {
      if (agentFilter && !agentFilter.has(agentId)) continue;
      this.queryAgent(agentId, t0, t1, out);
    }
    return out;
  }

  clear(): void {
    this.agents.clear();
    this.byId.clear();
    this.globalMaxEndMs = 0;
    this.emit({ agentId: null, t0: 0, t1: Number.POSITIVE_INFINITY });
  }
}

export interface DirtyRect {
  agentId: string | null; // null = global (e.g. clear, agent added)
  t0: number;
  t1: number;
}
