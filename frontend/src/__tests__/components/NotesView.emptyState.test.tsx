/**
 * Item 3 of UX cleanup batch — Notes view empty state CTA.
 *
 * The Notes panel used to render a near-blank empty state for sessions
 * with no annotations. The new empty state surfaces a clear "no notes
 * yet" message and a CTA pointing operators at the existing add-note
 * flow on the span popover.
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../components/shell/views/views.css', () => ({}));

const uiStoreState = {
  currentSessionId: 'sess-empty' as string | null,
  selectSpan: vi.fn(),
};
vi.mock('../../state/uiStore', () => ({
  useUiStore: <T,>(selector: (s: typeof uiStoreState) => T) =>
    selector(uiStoreState),
}));

const annStoreState = {
  bySession: new Map(),
  list: () => [],
};
vi.mock('../../state/annotationStore', () => ({
  useAnnotationStore: <T,>(selector: (s: typeof annStoreState) => T) =>
    selector(annStoreState),
}));

import { NotesView } from '../../components/shell/views/NotesView';

beforeEach(() => {
  uiStoreState.currentSessionId = 'sess-empty';
  annStoreState.bySession = new Map();
});

describe('<NotesView /> empty state CTA', () => {
  it('renders the empty-state CTA when the session has no notes', () => {
    render(<NotesView />);
    const cta = screen.getByTestId('notes-empty-cta');
    expect(cta).toBeInTheDocument();
    expect(cta).toHaveTextContent('No notes for this session yet.');
    // Hint copy directs operators to the existing add-note flow.
    expect(cta).toHaveTextContent(/Add note/i);
  });

  it('does not render the empty-state CTA when there is no session', () => {
    uiStoreState.currentSessionId = null;
    render(<NotesView />);
    expect(screen.queryByTestId('notes-empty-cta')).toBeNull();
    // Falls back to the generic "no session selected" hint.
    expect(screen.getByText(/no session selected/i)).toBeInTheDocument();
  });

  it('does not render the empty-state CTA when notes exist', () => {
    annStoreState.bySession = new Map([
      [
        'sess-empty',
        [
          {
            id: 'a1',
            sessionId: 'sess-empty',
            spanId: '',
            kind: 'COMMENT' as const,
            body: 'a note',
            author: 'tester',
            createdAtMs: 1000,
            pending: false,
            error: null,
          },
        ],
      ],
    ]);
    render(<NotesView />);
    expect(screen.queryByTestId('notes-empty-cta')).toBeNull();
    expect(screen.getByText('a note')).toBeInTheDocument();
  });
});
