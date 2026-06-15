import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SessionStore } from '../../gantt/index';

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
  type ZAgent,
} from '../../components/zicato/adapter';

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
