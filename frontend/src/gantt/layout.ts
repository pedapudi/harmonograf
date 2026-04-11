import type { Span } from './types';

// Greedy interval packing: assign each span the lowest lane with no overlap.
// Spans are processed in startMs order; lane end times are tracked in a small
// heap-less array (our lane counts are in the single-digits for typical agents,
// and the cost of a linear scan beats a heap at that size).
//
// This function mutates spans in place. It does NOT guarantee stable lanes
// across rebuilds — if you need stability, rebuild only affected agents.

export function packLanes(spans: Span[]): number {
  // Sort (stable): startMs, then endMs ascending, so nested spans get lane 1+
  // under their parent instead of pre-empting it.
  spans.sort((a, b) => {
    if (a.startMs !== b.startMs) return a.startMs - b.startMs;
    const ae = a.endMs ?? Number.POSITIVE_INFINITY;
    const be = b.endMs ?? Number.POSITIVE_INFINITY;
    return ae - be;
  });
  const laneEnds: number[] = [];
  let maxLane = 0;
  for (const s of spans) {
    let lane = -1;
    for (let i = 0; i < laneEnds.length; i++) {
      if (laneEnds[i] <= s.startMs) {
        lane = i;
        break;
      }
    }
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(0);
    }
    laneEnds[lane] = s.endMs ?? Number.POSITIVE_INFINITY;
    s.lane = lane;
    if (lane > maxLane) maxLane = lane;
  }
  return maxLane + 1;
}
