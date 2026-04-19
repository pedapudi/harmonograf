import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { applyGoldfiveEvent } from '../../rpc/goldfiveEvent';
import { SessionStore } from '../../gantt/index';
import {
  EventSchema,
  ApprovalRequestedSchema,
  ApprovalGrantedSchema,
} from '../../pb/goldfive/v1/events_pb';
import { useApprovalsStore } from '../../state/approvalsStore';

const sendSpy = vi.fn().mockResolvedValue(undefined);

vi.mock('../../rpc/hooks', () => ({
  useSendControl: () => sendSpy,
}));

let mockSessionId: string | null = 'sess-1';
vi.mock('../../state/uiStore', () => ({
  useUiStore: <T,>(selector: (s: { currentSessionId: string | null }) => T) =>
    selector({ currentSessionId: mockSessionId }),
}));

import { ApprovalDrawer } from '../../components/Interaction/ApprovalDrawer';

function synthRequest(opts: {
  sessionId?: string;
  targetId: string;
  kind?: string;
  prompt: string;
  taskId?: string;
  metadata?: Record<string, string>;
}) {
  const store = new SessionStore();
  applyGoldfiveEvent(
    create(EventSchema, {
      eventId: `ev-${opts.targetId}`,
      runId: 'run-1',
      sequence: 0n,
      payload: {
        case: 'approvalRequested',
        value: create(ApprovalRequestedSchema, {
          targetId: opts.targetId,
          kind: opts.kind ?? 'task',
          prompt: opts.prompt,
          taskId: opts.taskId ?? opts.targetId,
          metadata: opts.metadata ?? {},
        }),
      },
    }),
    store,
    0,
    opts.sessionId ?? 'sess-1',
  );
}

function synthGrant(targetId: string, sessionId = 'sess-1') {
  const store = new SessionStore();
  applyGoldfiveEvent(
    create(EventSchema, {
      eventId: `ev-g-${targetId}`,
      runId: 'run-1',
      sequence: 1n,
      payload: {
        case: 'approvalGranted',
        value: create(ApprovalGrantedSchema, { targetId, detail: 'ok' }),
      },
    }),
    store,
    0,
    sessionId,
  );
}

describe('<ApprovalDrawer />', () => {
  beforeEach(() => {
    useApprovalsStore.setState({ bySession: new Map() });
    mockSessionId = 'sess-1';
    sendSpy.mockClear();
  });
  afterEach(() => {
    useApprovalsStore.setState({ bySession: new Map() });
  });

  it('renders nothing when there is no current session', () => {
    mockSessionId = null;
    const { container } = render(<ApprovalDrawer />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when no approvals pending', () => {
    const { container } = render(<ApprovalDrawer />);
    expect(container.firstChild).toBeNull();
  });

  it('shows a card with the prompt and tool args for a pending tool-level approval', () => {
    act(() => {
      synthRequest({
        targetId: 'adk-1',
        kind: 'tool',
        prompt: 'Run write_file(path=/etc/passwd)?',
        taskId: 't-parent',
        metadata: {
          tool_name: 'write_file',
          args_json: '{"path":"/etc/passwd"}',
        },
      });
    });
    render(<ApprovalDrawer />);
    expect(screen.getByTestId('approval-drawer')).toBeInTheDocument();
    const card = screen.getByTestId('approval-card');
    expect(card).toHaveAttribute('data-target-id', 'adk-1');
    expect(card).toHaveAttribute('data-kind', 'tool');
    expect(screen.getByTestId('approval-prompt')).toHaveTextContent(
      'Run write_file(path=/etc/passwd)?',
    );
    expect(screen.getByTestId('approval-tool-name')).toHaveTextContent(
      'write_file',
    );
    // args_json is pretty-printed.
    expect(screen.getByTestId('approval-args').textContent).toContain(
      '/etc/passwd',
    );
  });

  it('clicking Approve dispatches APPROVE via sendControl with the targetId payload', async () => {
    act(() => {
      synthRequest({
        targetId: 'adk-2',
        kind: 'tool',
        prompt: 'ok?',
        taskId: 't-2',
      });
    });
    render(<ApprovalDrawer />);
    fireEvent.click(screen.getByTestId('approval-approve'));
    await waitFor(() => expect(sendSpy).toHaveBeenCalledTimes(1));
    const call = sendSpy.mock.calls[0][0];
    expect(call.kind).toBe('APPROVE');
    expect(call.sessionId).toBe('sess-1');
    // targetId is forwarded directly on the typed goldfive ApprovePayload
    // rather than via a JSON-encoded bytes payload.
    expect(call.targetId).toBe('adk-2');
  });

  it('clicking Reject dispatches REJECT via sendControl', async () => {
    act(() => {
      synthRequest({
        targetId: 't-3',
        kind: 'task',
        prompt: 'proceed?',
      });
    });
    render(<ApprovalDrawer />);
    fireEvent.click(screen.getByTestId('approval-reject'));
    await waitFor(() => expect(sendSpy).toHaveBeenCalledTimes(1));
    expect(sendSpy.mock.calls[0][0].kind).toBe('REJECT');
  });

  it('dismisses the card when the matching ApprovalGranted arrives', async () => {
    act(() => {
      synthRequest({ targetId: 't-4', kind: 'task', prompt: 'yes?' });
    });
    render(<ApprovalDrawer />);
    expect(screen.getByTestId('approval-card')).toBeInTheDocument();

    act(() => {
      synthGrant('t-4');
    });
    await waitFor(() =>
      expect(screen.queryByTestId('approval-card')).toBeNull(),
    );
    // With zero approvals the whole drawer collapses.
    expect(screen.queryByTestId('approval-drawer')).toBeNull();
  });

  it('surfaces dispatch errors inline and leaves the card open', async () => {
    sendSpy.mockRejectedValueOnce(new Error('network down'));
    act(() => {
      synthRequest({ targetId: 't-5', kind: 'task', prompt: 'proceed?' });
    });
    render(<ApprovalDrawer />);
    fireEvent.click(screen.getByTestId('approval-approve'));
    await waitFor(() =>
      expect(screen.getByTestId('approval-error')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('approval-error').textContent).toContain(
      'network down',
    );
    // Card stays open for retry.
    expect(screen.getByTestId('approval-card')).toBeInTheDocument();
  });

  it('shows multiple pending approvals at once, ordered by requestedAtMs', () => {
    act(() => {
      synthRequest({ targetId: 'a', prompt: 'first' });
      synthRequest({ targetId: 'b', prompt: 'second' });
    });
    render(<ApprovalDrawer />);
    const cards = screen.getAllByTestId('approval-card');
    expect(cards).toHaveLength(2);
    expect(cards[0]).toHaveAttribute('data-target-id', 'a');
    expect(cards[1]).toHaveAttribute('data-target-id', 'b');
  });
});
