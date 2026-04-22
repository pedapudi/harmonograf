import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PayloadRef } from '../../gantt/types';

// usePayload is the hook the ReasoningSection calls for large reasoning
// blobs. We swap it for a spy so tests can assert behavior without a real
// transport.
const usePayloadSpy = vi.fn<(digest: string | null) => {
  bytes: Uint8Array | null;
  mimeType: string;
  loading: boolean;
  error: string | null;
}>();

vi.mock('../../rpc/hooks', () => ({
  usePayload: (digest: string | null) => usePayloadSpy(digest),
}));

import { ReasoningSection } from '../../components/shell/Drawer';

beforeEach(() => {
  usePayloadSpy.mockReset();
  usePayloadSpy.mockReturnValue({
    bytes: null,
    mimeType: '',
    loading: false,
    error: null,
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('ReasoningSection', () => {
  it('is collapsed by default and does not request the payload until opened', () => {
    render(
      <ReasoningSection
        inline="inline reasoning trace"
        payloadRef={undefined}
      />,
    );
    // Body is not in the DOM until toggled open.
    expect(screen.queryByTestId('drawer-reasoning-body')).toBeNull();
    // Toggle exists with the Reasoning label.
    expect(screen.getByTestId('drawer-reasoning-toggle').textContent).toMatch(
      /Reasoning/i,
    );
  });

  it('renders the inline reasoning attribute when the toggle is opened', () => {
    render(
      <ReasoningSection
        inline="the model deliberated about step A then step B"
        payloadRef={undefined}
      />,
    );
    fireEvent.click(screen.getByTestId('drawer-reasoning-toggle'));
    const body = screen.getByTestId('drawer-reasoning-body');
    expect(body.textContent).toContain('step A then step B');
    // The inline path should not have asked for a payload fetch.
    expect(usePayloadSpy).toHaveBeenCalledWith(null);
  });

  it('fetches the payload by digest when reasoning rides as a payload_ref', async () => {
    const ref: PayloadRef = {
      digest: 'deadbeef1234',
      size: 4096,
      mime: 'text/plain',
      summary: 'long reasoning...',
      role: 'reasoning',
      evicted: false,
    };
    usePayloadSpy.mockImplementation((digest) => ({
      bytes:
        digest === 'deadbeef1234'
          ? new TextEncoder().encode('fetched long reasoning body')
          : null,
      mimeType: digest === 'deadbeef1234' ? 'text/plain' : '',
      loading: false,
      error: null,
    }));

    render(<ReasoningSection inline={undefined} payloadRef={ref} />);
    // Before opening: the hook is called with null (no fetch in flight).
    expect(usePayloadSpy).toHaveBeenCalledWith(null);

    fireEvent.click(screen.getByTestId('drawer-reasoning-toggle'));
    await waitFor(() => {
      expect(usePayloadSpy).toHaveBeenCalledWith('deadbeef1234');
    });
    const body = screen.getByTestId('drawer-reasoning-body');
    expect(body.textContent).toContain('fetched long reasoning body');
  });

  it('shows a loading indicator while the payload is in flight', () => {
    const ref: PayloadRef = {
      digest: 'dead',
      size: 4096,
      mime: 'text/plain',
      summary: '',
      role: 'reasoning',
      evicted: false,
    };
    usePayloadSpy.mockReturnValue({
      bytes: null,
      mimeType: '',
      loading: true,
      error: null,
    });
    render(<ReasoningSection inline={undefined} payloadRef={ref} />);
    fireEvent.click(screen.getByTestId('drawer-reasoning-toggle'));
    expect(screen.getByTestId('drawer-reasoning-body').textContent).toMatch(
      /loading reasoning/i,
    );
  });

  it('surfaces fetch errors from usePayload', () => {
    const ref: PayloadRef = {
      digest: 'dead',
      size: 4096,
      mime: 'text/plain',
      summary: '',
      role: 'reasoning',
      evicted: false,
    };
    usePayloadSpy.mockReturnValue({
      bytes: null,
      mimeType: '',
      loading: false,
      error: 'network exploded',
    });
    render(<ReasoningSection inline={undefined} payloadRef={ref} />);
    fireEvent.click(screen.getByTestId('drawer-reasoning-toggle'));
    expect(screen.getByTestId('drawer-reasoning-body').textContent).toContain(
      'network exploded',
    );
  });

  it('truncates display at 5000 chars and annotates the remainder', () => {
    const huge = 'a'.repeat(6000);
    render(<ReasoningSection inline={huge} payloadRef={undefined} />);
    fireEvent.click(screen.getByTestId('drawer-reasoning-toggle'));
    const body = screen.getByTestId('drawer-reasoning-body');
    expect(body.textContent).toMatch(/truncated \(1000 more chars\)/);
  });

  // --- Aggregate variant (harmonograf#108) --------------------------------

  it('relabels as "Agent reasoning trail" with a turn-count badge when isAggregate is set', () => {
    render(
      <ReasoningSection
        inline={'[LLM call 1]\nfirst\n\n---\n\n[LLM call 2]\nsecond'}
        payloadRef={undefined}
        callCount={2}
        isAggregate
      />,
    );
    const toggle = screen.getByTestId('drawer-reasoning-toggle');
    expect(toggle.textContent).toMatch(/Agent reasoning trail/);
    const badge = screen.getByTestId('drawer-reasoning-call-count');
    expect(badge.textContent).toBe('2 turns');
  });

  it('singularizes the turn-count badge for one LLM call', () => {
    render(
      <ReasoningSection
        inline={'only one turn'}
        payloadRef={undefined}
        callCount={1}
        isAggregate
      />,
    );
    expect(screen.getByTestId('drawer-reasoning-call-count').textContent).toBe(
      '1 turn',
    );
  });

  it('renders the concatenated trail body when opened', () => {
    const trail =
      '[LLM call 1]\nstep A reasoning\n\n---\n\n[LLM call 2]\nstep B reasoning';
    render(
      <ReasoningSection
        inline={trail}
        payloadRef={undefined}
        callCount={2}
        isAggregate
      />,
    );
    fireEvent.click(screen.getByTestId('drawer-reasoning-toggle'));
    const body = screen.getByTestId('drawer-reasoning-body');
    expect(body.textContent).toContain('step A reasoning');
    expect(body.textContent).toContain('step B reasoning');
    expect(body.textContent).toContain('[LLM call 1]');
    expect(body.textContent).toContain('[LLM call 2]');
  });

  it('omits the turn-count badge on per-LLM_CALL reasoning (no isAggregate, no count)', () => {
    render(
      <ReasoningSection
        inline={'single call reasoning'}
        payloadRef={undefined}
      />,
    );
    expect(screen.queryByTestId('drawer-reasoning-call-count')).toBeNull();
    expect(screen.getByTestId('drawer-reasoning-toggle').textContent).toMatch(
      /^▸Reasoning$/,
    );
  });
});
