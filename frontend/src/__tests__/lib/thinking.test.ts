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
  it('prefers llm.thought over other carriers', () => {
    const s = span('s1', {
      'llm.thought': str('primary'),
      thinking_text: str('secondary'),
      thinking_preview: str('preview'),
    });
    expect(extractThinkingText(s)).toBe('primary');
  });

  it('falls back to thinking_text when llm.thought missing', () => {
    const s = span('s1', { thinking_text: str('fallback') });
    expect(extractThinkingText(s)).toBe('fallback');
  });

  it('falls back to thinking_preview last', () => {
    const s = span('s1', { thinking_preview: str('tail') });
    expect(extractThinkingText(s)).toBe('tail');
  });

  it('returns null when no carrier present', () => {
    expect(extractThinkingText(span('s1'))).toBeNull();
  });
});

describe('hasThinking', () => {
  it('returns true when has_thinking=true even without text yet', () => {
    const s = span('s1', { has_thinking: bool(true) });
    expect(hasThinking(s)).toBe(true);
  });

  it('returns true when any thinking text is present', () => {
    const s = span('s1', { 'llm.thought': str('thinking') });
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
    const s1 = { ...span('a', { 'llm.thought': str('one') }), startMs: 200 };
    const s2 = { ...span('b', { 'llm.thought': str('two') }), startMs: 100 };
    const entries = collectThinkingEntries([s1, s2]);
    expect(entries.map((e) => e.spanId)).toEqual(['b', 'a']);
  });

  it('marks running spans as live', () => {
    const s = {
      ...span('live', { 'llm.thought': str('thinking') }),
      endMs: null,
    };
    const entries = collectThinkingEntries([s]);
    expect(entries[0].isLive).toBe(true);
  });

  it('skips spans without thinking content', () => {
    const s = span('no-think');
    expect(collectThinkingEntries([s])).toEqual([]);
  });
});

describe('collectThinkingForTask', () => {
  it('returns only entries whose hgraf.task_id attribute matches', () => {
    const inTask = span('in', {
      'llm.thought': str('inside'),
      'hgraf.task_id': str('task-1'),
    });
    const outTask = span('out', {
      'llm.thought': str('outside'),
      'hgraf.task_id': str('task-2'),
    });
    const unbound = span('unbound', { 'llm.thought': str('nomatch') });
    const entries = collectThinkingForTask([inTask, outTask, unbound], 'task-1');
    expect(entries).toHaveLength(1);
    expect(entries[0].spanId).toBe('in');
  });
});
