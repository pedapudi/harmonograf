// useReasoningText.test.tsx — the zicato reasoning resolver hook. Verifies the
// three carriers: inline string attribute (no fetch), a payload_ref blob
// (fetch + UTF-8 decode), and the in-flight loading state. usePayload is
// mocked so the test never stands up a real Connect transport.

import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PayloadRef, Span } from '../../gantt/types';

const usePayloadSpy = vi.fn<(digest: string | null) => {
  bytes: Uint8Array | null;
  mimeType: string;
  loading: boolean;
  error: string | null;
}>();

vi.mock('../../rpc/hooks', () => ({
  usePayload: (digest: string | null) => usePayloadSpy(digest),
}));

import { useReasoningText } from '../../components/zicato/useReasoningText';

function makeSpan(overrides: Partial<Span> = {}): Span {
  return {
    id: 'span-1',
    sessionId: 'sess-1',
    agentId: 'agent-1',
    parentSpanId: null,
    kind: 'LLM_CALL',
    status: 'COMPLETED',
    name: 'llm-call',
    startMs: 0,
    endMs: 100,
    links: [],
    attributes: {},
    payloadRefs: [],
    error: null,
    lane: 0,
    replaced: false,
    ...overrides,
  };
}

const reasoningRef: PayloadRef = {
  digest: 'deadbeef1234',
  size: 4096,
  mime: 'text/plain',
  summary: 'long reasoning...',
  role: 'reasoning',
  evicted: false,
};

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

describe('useReasoningText', () => {
  it('returns inline reasoning text without fetching a payload', () => {
    const span = makeSpan({
      attributes: {
        'llm.reasoning': {
          kind: 'string',
          value: 'the model deliberated about step A then step B',
        },
      },
      // A reasoning ref is also present, but inline wins and must not fetch.
      payloadRefs: [reasoningRef],
    });
    const { result } = renderHook(() => useReasoningText(span));
    expect(result.current).toEqual({
      text: 'the model deliberated about step A then step B',
      loading: false,
    });
    // Inline path must not chase the payload digest.
    expect(usePayloadSpy).toHaveBeenCalledWith(null);
    expect(usePayloadSpy).not.toHaveBeenCalledWith('deadbeef1234');
  });

  it('fetches and decodes the role:reasoning payload bytes when there is no inline text', () => {
    usePayloadSpy.mockImplementation((digest) => ({
      bytes:
        digest === 'deadbeef1234'
          ? new TextEncoder().encode('fetched long reasoning body')
          : null,
      mimeType: digest === 'deadbeef1234' ? 'text/plain' : '',
      loading: false,
      error: null,
    }));
    const span = makeSpan({ payloadRefs: [reasoningRef] });
    const { result } = renderHook(() => useReasoningText(span));
    expect(usePayloadSpy).toHaveBeenCalledWith('deadbeef1234');
    expect(result.current).toEqual({
      text: 'fetched long reasoning body',
      loading: false,
    });
  });

  it('reports loading while the payload-backed trace is in flight', () => {
    usePayloadSpy.mockReturnValue({
      bytes: null,
      mimeType: '',
      loading: true,
      error: null,
    });
    const span = makeSpan({ payloadRefs: [reasoningRef] });
    const { result } = renderHook(() => useReasoningText(span));
    expect(result.current).toEqual({ text: null, loading: true });
  });

  it('ignores non-reasoning payload refs and returns no text', () => {
    const span = makeSpan({
      payloadRefs: [{ ...reasoningRef, role: 'output' }],
    });
    const { result } = renderHook(() => useReasoningText(span));
    expect(usePayloadSpy).toHaveBeenCalledWith(null);
    expect(result.current).toEqual({ text: null, loading: false });
  });

  it('returns no text for a null span without fetching', () => {
    const { result } = renderHook(() => useReasoningText(null));
    expect(usePayloadSpy).toHaveBeenCalledWith(null);
    expect(result.current).toEqual({ text: null, loading: false });
  });
});
