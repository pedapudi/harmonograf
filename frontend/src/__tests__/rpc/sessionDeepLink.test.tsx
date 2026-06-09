// Coverage for the `#/session/<id>` deep-link routing added to App.tsx:
//   * sessionIdFromHash parses the hash form (and rejects others),
//   * mounting App with a `#/session/<id>` hash selects that session in the
//     UI store — even before the session has arrived from ListSessions, and
//     re-applies once the sessions list updates.
//
// Shell + StressPage are mocked to inert markers so the test exercises only
// the routing/selection logic without pulling in the whole UI tree.

import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { sessionIdFromHash } from '../../lib/sessionRoute';
import { useUiStore } from '../../state/uiStore';
import { useSessionsStore } from '../../state/sessionsStore';
import type { RpcSession } from '../../state/sessionsStore';

// Inert stand-ins — App's routing only needs to mount *something*.
vi.mock('../../components/shell/Shell', () => ({
  Shell: () => <div data-testid="shell" />,
}));
vi.mock('../../gantt/StressPage', () => ({
  StressPage: () => <div data-testid="stress" />,
}));

import App from '../../App';

function setHash(hash: string): void {
  window.location.hash = hash;
}

beforeEach(() => {
  useUiStore.setState({ currentSessionId: null });
  useSessionsStore.setState({ sessions: [], loading: false, error: null });
});

afterEach(() => {
  setHash('');
});

describe('sessionIdFromHash', () => {
  it('parses #/session/<id>', () => {
    expect(sessionIdFromHash('#/session/abc-123')).toBe('abc-123');
    expect(sessionIdFromHash('#/session/abc-123/')).toBe('abc-123');
  });

  it('url-decodes the id', () => {
    expect(sessionIdFromHash('#/session/run%2F42')).toBe('run/42');
  });

  it('returns null for non-session hashes', () => {
    expect(sessionIdFromHash('#/')).toBeNull();
    expect(sessionIdFromHash('#/stress')).toBeNull();
    expect(sessionIdFromHash('#/session/')).toBeNull();
    expect(sessionIdFromHash('')).toBeNull();
  });
});

describe('App #/session/<id> deep link', () => {
  it('selects the deep-linked session on mount, even before it loads', () => {
    setHash('#/session/sess-xyz');
    render(<App />);
    // Selected eagerly so SessionsSyncer's newest-first auto-select can't race.
    expect(useUiStore.getState().currentSessionId).toBe('sess-xyz');
  });

  it('keeps the deep-linked selection once the session appears in the list', () => {
    setHash('#/session/sess-late');
    render(<App />);
    expect(useUiStore.getState().currentSessionId).toBe('sess-late');

    act(() => {
      useSessionsStore.setState({
        sessions: [{ id: 'sess-late' } as RpcSession],
        loading: false,
        error: null,
      });
    });
    expect(useUiStore.getState().currentSessionId).toBe('sess-late');
  });

  it('does not bounce back to the deep link after a manual selection', () => {
    // Regression: the effect must apply the deep link once, not re-fire on
    // every ListSessions poll. Otherwise a user who deep-links to sess-a and
    // then picks sess-b (which updates the store, not the hash) gets dragged
    // back to sess-a on the next poll.
    setHash('#/session/sess-a');
    const { rerender } = render(<App />);
    expect(useUiStore.getState().currentSessionId).toBe('sess-a');

    // User picks another session via the picker.
    act(() => {
      useUiStore.getState().setCurrentSession('sess-b');
    });

    // A ListSessions poll lands (new sessions array) and App re-renders.
    act(() => {
      useSessionsStore.setState({
        sessions: [{ id: 'sess-a' } as RpcSession, { id: 'sess-b' } as RpcSession],
        loading: false,
        error: null,
      });
    });
    rerender(<App />);

    expect(useUiStore.getState().currentSessionId).toBe('sess-b');
  });

  it('does not select anything for the default route', () => {
    setHash('#/');
    render(<App />);
    expect(useUiStore.getState().currentSessionId).toBeNull();
  });
});
