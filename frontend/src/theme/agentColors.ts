import { schemeTableau10 } from 'd3-scale-chromatic';

// System-assigned agent row colors. Stable per agent_id via FNV-1a hash so the
// same agent always lands on the same color across reloads. Users can switch
// themes but cannot override individual agent colors (decision in doc 04 §6.3).

const palette: readonly string[] = schemeTableau10;

function fnv1a(str: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

export function colorForAgent(agentId: string): string {
  return palette[fnv1a(agentId) % palette.length];
}
