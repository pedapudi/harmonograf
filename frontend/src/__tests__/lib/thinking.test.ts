import { describe, it, expect } from 'vitest';
import type { AttributeValue, Span } from '../../gantt/types';
import {
  collectThinkingEntries,
  collectThinkingForTask,
  extractThinkingText,
  formatThinkingInline,
  formatThinkingPreview,
  hasThinking,
} from '../../lib/thinking';

function span(id: string, attrs: Record<string, AttributeValue> = {}): Span {
  return {
    id,
    sessionId: 'session-1',
    agentId: 'agent-a',
    parentSpanId: null,
    kind: 'LLM_CALL',
    status: 'COMPLETED',
    name: 'gemini-2.5-pro',
    startMs: 0,
    endMs: 100,
    links: [],
    attributes: attrs,
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
}

const str = (v: string): AttributeValue => ({ kind: 'string', value: v });
const bool = (v: boolean): AttributeValue => ({ kind: 'bool', value: v });

describe('extractThinkingText', () => {
  it('prefers the INVOCATION reasoning_trail aggregate over a single LLM_CALL reasoning', () => {
    const s = span('s1', {
      'llm.reasoning_trail': str('full agent trail'),
      'llm.reasoning': str('single call'),
    });
    expect(extractThinkingText(s)).toBe('full agent trail');
  });

  it('falls back to llm.reasoning when no aggregate is present', () => {
    const s = span('s1', { 'llm.reasoning': str('only-per-call') });
    expect(extractThinkingText(s)).toBe('only-per-call');
  });

  it('returns null when no reasoning carrier is present', () => {
    expect(extractThinkingText(span('s1'))).toBeNull();
  });
});

describe('hasThinking', () => {
  it('returns true when has_reasoning=true even without inline text', () => {
    const s = span('s1', { has_reasoning: bool(true) });
    expect(hasThinking(s)).toBe(true);
  });

  it('returns true when any reasoning text is present', () => {
    const s = span('s1', { 'llm.reasoning': str('thinking') });
    expect(hasThinking(s)).toBe(true);
  });

  it('returns true when the INVOCATION aggregate is present', () => {
    const s = span('s1', { 'llm.reasoning_trail': str('agent trail') });
    expect(hasThinking(s)).toBe(true);
  });

  it('returns false for a span with neither flag nor text', () => {
    expect(hasThinking(span('s1'))).toBe(false);
  });
});

describe('formatThinkingPreview', () => {
  it('returns an empty string for null input', () => {
    expect(formatThinkingPreview(null)).toBe('');
  });

  it('preserves short text untouched (except leading whitespace)', () => {
    expect(formatThinkingPreview('  hello world')).toBe('hello world');
  });

  it('truncates long text with an ellipsis', () => {
    const long = 'x'.repeat(500);
    const out = formatThinkingPreview(long, 50);
    expect(out.length).toBeLessThanOrEqual(50);
    expect(out.endsWith('…')).toBe(true);
  });
});

describe('formatThinkingInline', () => {
  it('collapses whitespace to spaces', () => {
    expect(formatThinkingInline('line1\n\n line2\t\tend')).toBe(
      'line1 line2 end',
    );
  });

  it('truncates at maxChars with ellipsis', () => {
    expect(formatThinkingInline('abcdefghij', 5)).toBe('abcd…');
  });
});

describe('collectThinkingEntries', () => {
  it('orders entries ascending by startMs', () => {
    const s1 = { ...span('a', { 'llm.reasoning': str('one') }), startMs: 200 };
    const s2 = { ...span('b', { 'llm.reasoning': str('two') }), startMs: 100 };
    const entries = collectThinkingEntries([s1, s2]);
    expect(entries.map((e) => e.spanId)).toEqual(['b', 'a']);
  });

  it('marks running spans as live', () => {
    const s = {
      ...span('live', { 'llm.reasoning': str('thinking') }),
      endMs: null,
    };
    const entries = collectThinkingEntries([s]);
    expect(entries[0].isLive).toBe(true);
  });

  it('skips spans without thinking content', () => {
    const s = span('no-think');
    expect(collectThinkingEntries([s])).toEqual([]);
  });

  it('picks up the INVOCATION aggregate on INVOCATION spans', () => {
    const s = span('inv', { 'llm.reasoning_trail': str('aggregate') });
    const entries = collectThinkingEntries([s]);
    expect(entries).toHaveLength(1);
    expect(entries[0].text).toBe('aggregate');
  });
});

describe('collectThinkingForTask', () => {
  it('returns only entries whose hgraf.task_id attribute matches', () => {
    const inTask = span('in', {
      'llm.reasoning': str('inside'),
      'hgraf.task_id': str('task-1'),
    });
    const outTask = span('out', {
      'llm.reasoning': str('outside'),
      'hgraf.task_id': str('task-2'),
    });
    const unbound = span('unbound', { 'llm.reasoning': str('nomatch') });
    const entries = collectThinkingForTask([inTask, outTask, unbound], 'task-1');
    expect(entries).toHaveLength(1);
    expect(entries[0].spanId).toBe('in');
  });
});
