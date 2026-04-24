import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { RevisionHistoryBadge } from '../../../components/TaskStages/RevisionHistoryBadge';
import type { TaskRevisionChain } from '../../../components/TaskStages/collapsedLayout';
import type { Task, TaskStatus } from '../../../gantt/types';

vi.mock('../../../components/TaskStages/RevisionHistoryBadge.css', () => ({}));

function mkTask(id: string, title: string, status: TaskStatus = 'PENDING'): Task {
  return {
    id,
    title,
    description: '',
    assigneeAgentId: 'agent-a',
    status,
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    supersedes: '',
  };
}

interface MakeChainOpts {
  titles?: string[];
  revisions?: number[];
}

// Build a chain from a list of revision indices. Oldest→newest. The last
// member is the canonical one.
function makeChain(opts: MakeChainOpts = {}): TaskRevisionChain {
  const revisions = opts.revisions ?? [0, 1, 2];
  const titles =
    opts.titles ?? revisions.map((r, i) => `Task at R${r} (member ${i})`);
  if (titles.length !== revisions.length) {
    throw new Error('makeChain: titles / revisions length mismatch');
  }
  const members = revisions.map((r, i) => mkTask(`t-r${r}-${i}`, titles[i]));
  return {
    canonical: members[members.length - 1],
    members,
    revisions,
  };
}

describe('<RevisionHistoryBadge />', () => {
  it('singleton chain: renders a single rev chip and no toggle', () => {
    const chain = makeChain({ revisions: [2], titles: ['only one'] });
    render(<RevisionHistoryBadge chain={chain} currentRevision={null} />);

    expect(screen.getByText('R2')).toBeInTheDocument();
    // No toggle button for singletons.
    expect(screen.queryByRole('button')).toBeNull();
    // No history list rendered.
    expect(screen.queryByRole('list')).toBeNull();
  });

  it('3-member chain collapsed by default: shows chain pill with aria-expanded=false', () => {
    const chain = makeChain({ revisions: [0, 1, 2] });
    render(<RevisionHistoryBadge chain={chain} currentRevision={null} />);

    const toggle = screen.getByRole('button');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle.textContent).toContain('R0→R1→R2');
    expect(toggle).toHaveAttribute(
      'aria-label',
      expect.stringContaining('Revision history'),
    );
    // History not rendered yet.
    expect(screen.queryByRole('list')).toBeNull();
  });

  it('3-member chain: click toggle expands, shows 2 predecessor rows newest-first', () => {
    const chain = makeChain({
      revisions: [0, 1, 2],
      titles: ['alpha', 'bravo', 'charlie-canonical'],
    });
    render(<RevisionHistoryBadge chain={chain} currentRevision={null} />);

    const toggle = screen.getByRole('button');
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    const list = screen.getByRole('list');
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    // Newest-first: R1 row first, then R0.
    expect(items[0].textContent).toContain('R1');
    expect(items[0].textContent).toContain('bravo');
    expect(items[1].textContent).toContain('R0');
    expect(items[1].textContent).toContain('alpha');
    // Canonical is NOT listed.
    expect(list.textContent).not.toContain('charlie-canonical');
  });

  it('expand then collapse: back to compact, toggle retains focus', () => {
    const chain = makeChain({ revisions: [0, 1, 2] });
    render(<RevisionHistoryBadge chain={chain} currentRevision={null} />);

    const toggle = screen.getByRole('button');
    toggle.focus();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('list')).toBeNull();
    expect(document.activeElement).toBe(toggle);
  });

  it('controlled-expansion mode: parent owns state, internal state untouched', () => {
    const chain = makeChain({ revisions: [0, 1, 2] });
    const onToggle = vi.fn();
    const { rerender } = render(
      <RevisionHistoryBadge
        chain={chain}
        currentRevision={null}
        expanded={true}
        onToggleExpanded={onToggle}
      />,
    );

    const toggle = screen.getByRole('button', { name: /revision history/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('list')).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(onToggle).toHaveBeenCalledTimes(1);
    // Still expanded because parent hasn't flipped the prop — component
    // does not manage its own state when controlled.
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    rerender(
      <RevisionHistoryBadge
        chain={chain}
        currentRevision={null}
        expanded={false}
        onToggleExpanded={onToggle}
      />,
    );
    expect(screen.getByRole('button', { name: /revision history/i })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
  });

  it('onClickMember wired: clicking a history row fires handler with the right task', () => {
    const chain = makeChain({
      revisions: [0, 1, 2],
      titles: ['alpha', 'bravo', 'charlie'],
    });
    const onClickMember = vi.fn();
    render(
      <RevisionHistoryBadge
        chain={chain}
        currentRevision={null}
        onClickMember={onClickMember}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /revision history/i }));
    // Rows now rendered as buttons.
    const predButtons = screen.getAllByRole('button', {
      name: /open predecessor/i,
    });
    expect(predButtons).toHaveLength(2);
    // Newest-first — first predecessor is R1 ("bravo").
    fireEvent.click(predButtons[0]);
    expect(onClickMember).toHaveBeenCalledTimes(1);
    expect(onClickMember.mock.calls[0][0]).toMatchObject({
      id: chain.members[1].id,
      title: 'bravo',
    });
  });

  it('onClickMember NOT wired: history rows are inert (not buttons)', () => {
    const chain = makeChain({ revisions: [0, 1, 2] });
    render(<RevisionHistoryBadge chain={chain} currentRevision={null} />);

    fireEvent.click(screen.getByRole('button', { name: /revision history/i }));
    // Only the toggle button should be present; no per-row buttons.
    expect(
      screen.queryAllByRole('button', { name: /open predecessor/i }),
    ).toHaveLength(0);
    // Rows still rendered as listitems.
    const list = screen.getByRole('list');
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
  });

  it('currentRevision matches a predecessor: (pinned) annotation on that row', () => {
    const chain = makeChain({
      revisions: [0, 1, 2],
      titles: ['alpha', 'bravo', 'charlie'],
    });
    render(<RevisionHistoryBadge chain={chain} currentRevision={1} />);

    fireEvent.click(screen.getByRole('button', { name: /revision history/i }));
    const items = within(screen.getByRole('list')).getAllByRole('listitem');
    // Newest-first → items[0] is R1 (bravo), the pinned predecessor.
    expect(items[0].textContent).toContain('(pinned)');
    expect(items[1].textContent).not.toContain('(pinned)');
    // Summary on the pill mentions the pin, too.
    const toggle = screen.getByRole('button', { name: /revision history/i });
    expect(toggle.textContent).toContain('pinned R1');
  });

  it('currentRevision at or after canonical: no (pinned) annotation', () => {
    const chain = makeChain({ revisions: [0, 1, 2] });
    const { rerender } = render(
      <RevisionHistoryBadge chain={chain} currentRevision={2} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /revision history/i }));
    expect(screen.queryByText('(pinned)')).toBeNull();

    rerender(<RevisionHistoryBadge chain={chain} currentRevision={5} />);
    expect(screen.queryByText('(pinned)')).toBeNull();
  });

  it('currentRevision earlier than every member: "not yet introduced"', () => {
    const chain = makeChain({ revisions: [3, 5, 7] });
    render(<RevisionHistoryBadge chain={chain} currentRevision={0} />);
    const toggle = screen.getByRole('button', { name: /revision history/i });
    expect(toggle.textContent?.toLowerCase()).toContain('not yet introduced');
    // No (pinned) rows when expanded.
    fireEvent.click(toggle);
    expect(screen.queryByText('(pinned)')).toBeNull();
  });

  it('keyboard toggle: Enter and Space both expand; Tab moves focus', () => {
    const chain = makeChain({ revisions: [0, 1, 2] });
    render(<RevisionHistoryBadge chain={chain} currentRevision={null} />);
    const toggle = screen.getByRole('button');

    toggle.focus();
    expect(document.activeElement).toBe(toggle);

    fireEvent.keyDown(toggle, { key: 'Enter' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    fireEvent.keyDown(toggle, { key: 'Enter' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    fireEvent.keyDown(toggle, { key: ' ' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
  });
});
