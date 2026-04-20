import { schemeTableau10 } from 'd3-scale-chromatic';

// System-assigned agent row colors. Stable per agent_id via FNV-1a hash so the
// same agent always lands on the same color across reloads. Users can switch
// themes but cannot override individual agent colors (decision in doc 04 §6.3).

const palette: readonly string[] = schemeTableau10;

// Synthetic-actor agent IDs. These rows represent things that *act on* the run
// from outside the worker agents — the human operator and the goldfive
// orchestrator — so their drifts and steers appear as first-class spans in
// every view. The double-underscore prefix is reserved; real agent IDs
// should never collide.
export const USER_ACTOR_ID = '__user__';
export const GOLDFIVE_ACTOR_ID = '__goldfive__';

export const SYNTHETIC_ACTOR_IDS = new Set<string>([
  USER_ACTOR_ID,
  GOLDFIVE_ACTOR_ID,
]);

// Fixed colors for the synthetic actors — outside the hashed palette so a real
// agent's color never shadows them. Picked to read as "person" (warm neutral)
// and "orchestrator" (cool / desaturated) at a glance.
const USER_COLOR = '#d0bcff';
const GOLDFIVE_COLOR = '#80deea';

export function colorForAgent(agentId: string): string {
  if (agentId === USER_ACTOR_ID) return USER_COLOR;
  if (agentId === GOLDFIVE_ACTOR_ID) return GOLDFIVE_COLOR;
  return palette[fnv1a(agentId) % palette.length];
}

export function isSyntheticActor(agentId: string): boolean {
  return SYNTHETIC_ACTOR_IDS.has(agentId);
}

export function actorDisplayLabel(agentId: string): string | null {
  if (agentId === USER_ACTOR_ID) return 'user';
  if (agentId === GOLDFIVE_ACTOR_ID) return 'goldfive';
  return null;
}

function fnv1a(str: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}
