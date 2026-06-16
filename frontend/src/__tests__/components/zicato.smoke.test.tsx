import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Span } from '../../gantt/types';

// Keep the smoke test hermetic: no real RPC, no global key handlers, no
// session-list poll. The console must render against an empty store without
// throwing — that is the scaffold gate.
const emptyStore = new SessionStore();

vi.mock('../../rpc/hooks', () => ({
  useSessionWatch: () => ({
    store: emptyStore,
    connected: false,
    initialBurstComplete: false,
    error: null,
    sessionStatus: 'UNKNOWN',
    lastEventAtMs: 0,
  }),
  getSessionStore: () => undefined,
  useSendControl: () => async () => {},
}));
vi.mock('../../rpc/SessionsSyncer', () => ({ SessionsSyncer: () => null }));
vi.mock('../../components/SessionPicker/SessionPicker', () => ({
  SessionPicker: () => null,
}));
vi.mock('../../lib/shortcuts', () => ({ useGlobalShortcuts: () => {} }));

import { ZicatoConsole } from '../../components/zicato/ZicatoConsole';
import {
  EMPTY_SESSION,
  toKindToken,
  toStatusToken,
  colorVar,
  buildSpans,
  buildSteers,
  type ZAgent,
} from '../../components/zicato/adapter';
import { steerColor } from '../../components/zicato/svgUtils';

describe('zicato console scaffold', () => {
  it('renders ZicatoConsole against an empty store without throwing', () => {
    render(<ZicatoConsole />);
    expect(screen.getByTestId('zicato-console')).toBeTruthy();
  });

  it('mounts the md3 toggle so the user can switch back', () => {
    render(<ZicatoConsole />);
    expect(screen.getByTestId('ui-mode-toggle-z')).toBeTruthy();
  });

  it('renders both rail views (gantt + instruments)', () => {
    render(<ZicatoConsole />);
    // Two rail items, each labelled by its view name.
    expect(screen.getByText('gantt')).toBeTruthy();
    expect(screen.getByText('instruments')).toBeTruthy();
  });

  it('exposes a safe EMPTY_SESSION shape (empty arrays, empty:true)', () => {
    expect(EMPTY_SESSION.empty).toBe(true);
    expect(EMPTY_SESSION.spans).toEqual([]);
    expect(EMPTY_SESSION.agents).toEqual([]);
    expect(EMPTY_SESSION.edges).toEqual([]);
    expect(EMPTY_SESSION.transfers).toEqual([]);
    expect(EMPTY_SESSION.ladder).toEqual([]);
    expect(EMPTY_SESSION.ctx).toEqual([]);
    expect(EMPTY_SESSION.judges).toEqual({});
    expect(EMPTY_SESSION.ticks).toEqual({});
    expect(EMPTY_SESSION.plan.planId).toBeNull();
    expect(EMPTY_SESSION.delegation).toBeNull();
    expect(EMPTY_SESSION.T).toBe(30);
    expect(EMPTY_SESSION.now).toBe(0);
  });

  it('normalizes kind + status tokens', () => {
    expect(toKindToken('LLM_CALL')).toBe('llm-call');
    expect(toKindToken('WAIT_FOR_HUMAN')).toBe('wait-for-human');
    expect(toStatusToken('AWAITING_HUMAN')).toBe('awaiting');
    expect(toStatusToken('RUNNING')).toBe('running');
  });

  it('maps agents to the --hg-agent-* token ramp', () => {
    const a: ZAgent = { id: 'c:coder', label: 'coder', ordinal: 1, synthetic: null };
    const user: ZAgent = { id: '__user__', label: 'user', ordinal: 0, synthetic: 'user' };
    const gf: ZAgent = {
      id: '__goldfive__',
      label: 'goldfive',
      ordinal: 0,
      synthetic: 'goldfive',
    };
    expect(colorVar(a)).toBe('var(--hg-agent-1)');
    expect(colorVar(user)).toBe('var(--hg-agent-user)');
    expect(colorVar(gf)).toBe('var(--hg-agent-goldfive)');
  });
});

// ── reasoning + steering adapter logic (TRAJECTORY features) ─────────────────

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

describe('zicato adapter — reasoning detection (🧠)', () => {
  it('marks a span with llm.reasoning as hasReasoning + carries the text', () => {
    const store = new SessionStore();
    store.spans.append(
      mkSpan({
        id: 'sp-reason',
        agentId: 'c:coder',
        attributes: {
          'llm.reasoning': { kind: 'string', value: 'I will read the file first.' },
        },
      }),
    );
    const spans = buildSpans(store, [], 2);
    const sp = spans.find((s) => s.id === 'sp-reason')!;
    expect(sp.hasReasoning).toBe(true);
    expect(sp.reasoning).toBe('I will read the file first.');
  });

  it('honours the has_reasoning flag even when the text rides a payload ref', () => {
    const store = new SessionStore();
    store.spans.append(
      mkSpan({
        id: 'sp-flag',
        agentId: 'c:coder',
        attributes: { has_reasoning: { kind: 'bool', value: true } },
      }),
    );
    const sp = buildSpans(store, [], 2).find((s) => s.id === 'sp-flag')!;
    expect(sp.hasReasoning).toBe(true);
    // No inline text → null (the drawer renders the payload-ref fallback).
    expect(sp.reasoning).toBeNull();
  });

  it('prefers the INVOCATION reasoning_trail aggregate over a single call', () => {
    const store = new SessionStore();
    store.spans.append(
      mkSpan({
        id: 'sp-trail',
        agentId: 'c:coder',
        kind: 'INVOCATION',
        attributes: {
          'llm.reasoning': { kind: 'string', value: 'one call' },
          'llm.reasoning_trail': {
            kind: 'string',
            value: '[LLM call 1] one call\n[LLM call 2] two call',
          },
        },
      }),
    );
    const sp = buildSpans(store, [], 2).find((s) => s.id === 'sp-trail')!;
    expect(sp.hasReasoning).toBe(true);
    expect(sp.reasoning).toContain('[LLM call 2]');
  });

  it('leaves ordinary spans without reasoning', () => {
    const store = new SessionStore();
    store.spans.append(mkSpan({ id: 'sp-plain', agentId: 'c:coder' }));
    const sp = buildSpans(store, [], 2).find((s) => s.id === 'sp-plain')!;
    expect(sp.hasReasoning).toBe(false);
    expect(sp.reasoning).toBeNull();
  });
});

describe('zicato adapter — steering derivation (correction → target)', () => {
  it('derives a steer from a refine attempt pointing at the steered agent', () => {
    const store = new SessionStore();
    // A refine the orchestrator emitted in response to a drift, steering coder.
    store.refineAttempts.append({
      runId: 'r1',
      attemptId: 'att-1',
      driftId: 'drift-1',
      triggerKind: 'off_topic',
      triggerSeverity: 'warning',
      taskId: 't2',
      agentId: 'c:coder',
      recordedAtMs: 5000,
    });
    const steers = buildSteers(store, []);
    expect(steers).toHaveLength(1);
    expect(steers[0].to).toBe('c:coder');
    expect(steers[0].t).toBe(5); // ms → seconds
    expect(steers[0].kind).toBe('off_topic');
    expect(steers[0].severity).toBe('warning');
    expect(steers[0].taskId).toBe('t2');
  });

  it('resolves the target from the refine span target_agent_id when a revision matches', () => {
    const store = new SessionStore();
    // History: rev 1 triggered by drift-1.
    const history = [
      {
        revision: 1,
        kind: 'off_topic',
        reason: 'pulled back on task',
        triggerEventId: 'drift-1',
        // plan + record fields the adapter does not read in buildSteers:
      } as unknown as Parameters<typeof buildSteers>[1][number],
    ];
    // refine span carries the authoritative target agent for refine.index 1.
    store.spans.append(
      mkSpan({
        id: 'refine-1',
        agentId: '__goldfive__',
        kind: 'CUSTOM',
        name: 'refine: off_topic',
        attributes: {
          'refine.index': { kind: 'string', value: '1' },
          'refine.target_agent_id': { kind: 'string', value: 'c:reviewer' },
        },
      }),
    );
    store.refineAttempts.append({
      runId: 'r1',
      attemptId: 'att-1',
      driftId: 'drift-1',
      triggerKind: 'off_topic',
      triggerSeverity: 'warning',
      taskId: '',
      agentId: 'c:coder', // attempt's own agent — overridden by the span target
      recordedAtMs: 5000,
    });
    const steers = buildSteers(store, history);
    expect(steers).toHaveLength(1);
    // refine span's target_agent_id wins over the attempt's agentId.
    expect(steers[0].to).toBe('c:reviewer');
    expect(steers[0].revision).toBe(1);
    expect(steers[0].reason).toBe('pulled back on task');
  });

  it('falls back to drift→revision when no refine attempts were recorded', () => {
    const store = new SessionStore();
    store.drifts.append({
      kind: 'looping_reasoning',
      severity: 'critical',
      detail: 'repeated calls',
      taskId: 't1',
      agentId: 'c:coder',
      recordedAtMs: 8000,
      annotationId: '',
      driftId: 'drift-9',
    });
    const history = [
      {
        revision: 2,
        kind: 'looping_reasoning',
        reason: 'broke the loop',
        triggerEventId: 'drift-9',
      } as unknown as Parameters<typeof buildSteers>[1][number],
    ];
    const steers = buildSteers(store, history);
    expect(steers).toHaveLength(1);
    expect(steers[0].to).toBe('c:coder');
    expect(steers[0].severity).toBe('critical');
    expect(steers[0].revision).toBe(2);
  });

  it('drops a steer with no resolvable, non-goldfive target', () => {
    const store = new SessionStore();
    // Drift with no plan revision (no history match) and no refine attempts →
    // nothing to point at.
    store.drifts.append({
      kind: 'off_topic',
      severity: 'info',
      detail: '',
      taskId: '',
      agentId: '',
      recordedAtMs: 3000,
      annotationId: '',
      driftId: 'drift-x',
    });
    expect(buildSteers(store, [])).toEqual([]);
  });
});

describe('zicato — steering arrow hue by severity', () => {
  it('maps severity → correction token', () => {
    expect(steerColor({ severity: 'critical' })).toBe('var(--bad)');
    expect(steerColor({ severity: 'warning' })).toBe('var(--caution)');
    expect(steerColor({ severity: 'info' })).toBe('var(--hg-gf-refine)');
    expect(steerColor({ severity: '' })).toBe('var(--hg-gf-refine)');
  });
});
