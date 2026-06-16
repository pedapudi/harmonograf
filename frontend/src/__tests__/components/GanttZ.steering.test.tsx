// End-to-end render proof for the two TRAJECTORY features ported into the
// zicato Gantt: the goldfive STEERING arrow and the reasoning 🧠 glyph.
//
// Where zicato.smoke.test covers the *derivation* (buildSteers / buildSpans off
// a real SessionStore — the same wire path the MD3 console ingests), this test
// drives the WHOLE port: a real SessionStore → the adapter mappers → a live
// GanttZ render, asserting the SVG actually paints the arrow + the glyph. It is
// the deterministic stand-in for the headless deep-link screenshot (which only
// flakily catches the WatchSession stream after it finishes painting).
//
// Reference: the steering data shape mirrors src/__tests__/rpc/refineEvents.test
// (RefineAttempted → store.refineAttempts) and the MD3 GraphView interventions.

import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Span } from '../../gantt/types';
import {
  buildAgents,
  buildSpans,
  buildSteers,
  EMPTY_SESSION,
  type ZSession,
} from '../../components/zicato/adapter';
import { GanttZ } from '../../components/zicato/GanttZ';

const GOLDFIVE = 'presentation-orchestrated-abc:goldfive';
const CODER = 'presentation-orchestrated-abc:coder';

function mkSpan(over: Partial<Span> & Pick<Span, 'id' | 'agentId'>): Span {
  return {
    sessionId: 's',
    parentSpanId: null,
    kind: 'LLM_CALL',
    status: 'COMPLETED',
    name: over.name ?? over.id,
    startMs: 0,
    endMs: 1000,
    links: [],
    attributes: {},
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
    ...over,
  };
}

// Compose the live ZSession the way useZicatoSession does — real store → mappers
// → bundle — so the render exercises the actual port, not a hand-built fixture.
function sessionFromStore(store: SessionStore, history = []): ZSession {
  const agents = buildAgents(store);
  const now = 12;
  return {
    ...EMPTY_SESSION,
    id: 'steer-render',
    empty: false,
    T: 12,
    now,
    agents,
    spans: buildSpans(store, agents, now),
    steers: buildSteers(store, history),
  };
}

describe('GanttZ — steering arrow + reasoning glyph render', () => {
  function populatedStore(): SessionStore {
    const store = new SessionStore();
    // Two lanes: the goldfive orchestrator (arrow origin) + the work agent it
    // steers (arrow lands here). resolveGoldfiveActorId() returns the compound
    // `:goldfive` id once registered, matching buildSteers' `from`.
    store.agents.ensureAgent(GOLDFIVE, 'goldfive');
    store.agents.ensureAgent(CODER, 'coder');

    // A coder span carrying chain-of-thought → drives the 🧠 glyph.
    store.spans.append(
      mkSpan({
        id: 'sp-think',
        agentId: CODER,
        startMs: 1000,
        endMs: 9000,
        attributes: {
          'llm.reasoning': {
            kind: 'string',
            value: 'The task drifted; refocusing on the spec.',
          },
        },
      }),
    );

    // A refine the orchestrator emitted in response to a drift, steering coder.
    store.refineAttempts.append({
      runId: 'r1',
      attemptId: 'att-1',
      driftId: 'drift-1',
      triggerKind: 'off_topic',
      triggerSeverity: 'warning',
      taskId: 't2',
      agentId: CODER,
      recordedAtMs: 5000,
    });
    return store;
  }

  it('derives exactly one steer (goldfive → coder) and one reasoning span', () => {
    const z = sessionFromStore(populatedStore());
    expect(z.steers).toHaveLength(1);
    expect(z.steers[0].from).toBe(GOLDFIVE);
    expect(z.steers[0].to).toBe(CODER);
    expect(z.spans.filter((s) => s.hasReasoning)).toHaveLength(1);
  });

  it('paints a .is-steer arrow from the goldfive lane to the steered agent', () => {
    const z = sessionFromStore(populatedStore());
    const { container } = render(<GanttZ z={z} />);
    const arrow = container.querySelector('.hg-gantt-edge.is-steer');
    expect(arrow).not.toBeNull();
    // Severity → hue is applied inline (CSS class can't beat it). warning → caution.
    expect((arrow as SVGPathElement).getAttribute('style') ?? '').toContain(
      '--caution',
    );
    // The arrow's tooltip names the correction + its target.
    expect(
      Array.from(container.querySelectorAll('title')).some((t) =>
        (t.textContent ?? '').includes('steer'),
      ),
    ).toBe(true);
  });

  it('paints the 🧠 glyph on the span that carries reasoning', () => {
    const z = sessionFromStore(populatedStore());
    const { container } = render(<GanttZ z={z} />);
    const glyphs = Array.from(
      container.querySelectorAll('.hg-gantt-glyph-reason'),
    );
    expect(glyphs).toHaveLength(1);
    expect(glyphs[0].textContent).toBe('🧠');
  });

  it('does NOT paint the steer arrow when it falls outside the zoom window', () => {
    const z = sessionFromStore(populatedStore());
    // A window that excludes the steer time (t=5s) → the arrow is clipped out.
    const { container } = render(
      <GanttZ z={z} view={{ t0: 8, t1: 12 }} onViewChange={() => {}} />,
    );
    expect(container.querySelector('.hg-gantt-edge.is-steer')).toBeNull();
  });

  it('mini mode drops both the steer arrow and the glyph (minimap density)', () => {
    const z = sessionFromStore(populatedStore());
    const { container } = render(<GanttZ z={z} mini />);
    expect(container.querySelector('.hg-gantt-edge.is-steer')).toBeNull();
    expect(container.querySelector('.hg-gantt-glyph-reason')).toBeNull();
  });
});
