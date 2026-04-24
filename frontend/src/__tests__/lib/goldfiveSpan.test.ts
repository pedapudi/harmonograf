// Unit tests for the goldfive span helper library (harmonograf#157).
//
// Covers the detection + classification + color-resolution path that the
// renderer + popover + drawer all depend on. Kept separate from the
// component tests so the pure-function behaviour (category buckets,
// verdict classification, preview truncation, fallback strings) is
// testable without a DOM.

import { describe, expect, it } from 'vitest';
import type { AttributeValue, Span } from '../../gantt/types';
import {
  bareGoldfiveAgentName,
  goldfiveCallFill,
  isGoldfiveSpan,
  resolveGoldfiveSpanInfo,
  truncatePreview,
} from '../../lib/goldfiveSpan';

function attr(value: string): AttributeValue {
  return { kind: 'string', value };
}

function boolAttr(value: boolean): AttributeValue {
  return { kind: 'bool', value };
}

function mkSpan(overrides: Partial<Span> = {}): Span {
  return {
    id: 'sp-1',
    sessionId: 'sess',
    agentId: 'client-1:goldfive',
    parentSpanId: null,
    kind: 'CUSTOM',
    status: 'COMPLETED',
    name: 'refine_steer',
    startMs: 0,
    endMs: 100,
    lane: 0,
    attributes: {},
    payloadRefs: [],
    links: [],
    replaced: false,
    error: null,
    ...overrides,
  };
}

function stubCssVar(custom: Record<string, string> = {}): (name: string) => string {
  return (name: string) => custom[name] ?? '';
}

describe('isGoldfiveSpan', () => {
  it('accepts a compound :goldfive agent id', () => {
    expect(isGoldfiveSpan(mkSpan({ agentId: 'client-x:goldfive' }))).toBe(true);
  });

  it('accepts the legacy __goldfive__ synthetic actor', () => {
    expect(isGoldfiveSpan(mkSpan({ agentId: '__goldfive__' }))).toBe(true);
  });

  it('accepts a span on any agent as long as goldfive.call_name is set', () => {
    expect(
      isGoldfiveSpan(
        mkSpan({
          agentId: 'agent-a',
          attributes: { 'goldfive.call_name': attr('judge_reasoning') },
        }),
      ),
    ).toBe(true);
  });

  it('rejects a normal worker-agent span', () => {
    expect(isGoldfiveSpan(mkSpan({ agentId: 'client:agent-a' }))).toBe(false);
  });

  it('rejects null / undefined', () => {
    expect(isGoldfiveSpan(null)).toBe(false);
    expect(isGoldfiveSpan(undefined)).toBe(false);
  });
});

describe('resolveGoldfiveSpanInfo — category classification', () => {
  it('classifies judge_* as judge', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({ attributes: { 'goldfive.call_name': attr('judge_reasoning') } }),
    );
    expect(info.category).toBe('judge');
    expect(info.callName).toBe('judge_reasoning');
  });

  it('classifies refine_* as refine', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({ attributes: { 'goldfive.call_name': attr('refine_steer') } }),
    );
    expect(info.category).toBe('refine');
  });

  it('classifies goal_derive + plan_generate as plan', () => {
    expect(
      resolveGoldfiveSpanInfo(
        mkSpan({ attributes: { 'goldfive.call_name': attr('goal_derive') } }),
      ).category,
    ).toBe('plan');
    expect(
      resolveGoldfiveSpanInfo(
        mkSpan({ attributes: { 'goldfive.call_name': attr('plan_generate') } }),
      ).category,
    ).toBe('plan');
  });

  it('classifies reflective_check as reflective', () => {
    expect(
      resolveGoldfiveSpanInfo(
        mkSpan({ attributes: { 'goldfive.call_name': attr('reflective_check') } }),
      ).category,
    ).toBe('reflective');
  });

  it('falls back to unknown for unrecognised call names', () => {
    expect(
      resolveGoldfiveSpanInfo(
        mkSpan({
          name: 'something_else',
          attributes: { 'goldfive.call_name': attr('novel_call_name') },
        }),
      ).category,
    ).toBe('unknown');
  });

  it('treats legacy refine: / judge: span names as refine / judge', () => {
    expect(resolveGoldfiveSpanInfo(mkSpan({ name: 'refine: looping' })).category).toBe(
      'refine',
    );
    expect(resolveGoldfiveSpanInfo(mkSpan({ name: 'judge: reasoning' })).category).toBe(
      'judge',
    );
  });

  it('returns callName = span.name when goldfive.call_name is absent', () => {
    const info = resolveGoldfiveSpanInfo(mkSpan({ name: 'refine: drift' }));
    expect(info.callName).toBe('refine: drift');
  });
});

describe('resolveGoldfiveSpanInfo — judge verdict classification', () => {
  it('verdict on_task when judge.on_task=true', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('judge_reasoning'),
          'judge.on_task': boolAttr(true),
        },
      }),
    );
    expect(info.verdict).toBe('on_task');
    expect(info.onTask).toBe(true);
  });

  it('verdict off_task_critical when severity=critical', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('judge_reasoning'),
          'judge.on_task': boolAttr(false),
          'judge.severity': attr('CRITICAL'),
        },
      }),
    );
    expect(info.verdict).toBe('off_task_critical');
    expect(info.severity).toBe('critical');
  });

  it('verdict off_task_warning when severity=warning', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('judge_reasoning'),
          'judge.on_task': boolAttr(false),
          'judge.severity': attr('warning'),
        },
      }),
    );
    expect(info.verdict).toBe('off_task_warning');
  });

  it('verdict no_verdict when there is no severity and no verdict string', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('judge_reasoning'),
        },
      }),
    );
    expect(info.verdict).toBe('no_verdict');
  });

  it('non-judge spans always report no_verdict', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('refine_steer'),
          // Even with a stray judge.severity the verdict stays no_verdict.
          'judge.severity': attr('critical'),
        },
      }),
    );
    expect(info.verdict).toBe('no_verdict');
  });
});

describe('resolveGoldfiveSpanInfo — target + preview reads', () => {
  it('strips the <client>: prefix from target_agent_id and keeps the raw form', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('refine_steer'),
          'goldfive.target_agent_id': attr('client-42:research_agent'),
        },
      }),
    );
    expect(info.targetAgentId).toBe('research_agent');
    expect(info.targetAgentIdRaw).toBe('client-42:research_agent');
  });

  it('leaves a non-compound target_agent_id untouched', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('refine_steer'),
          'goldfive.target_agent_id': attr('research_agent'),
        },
      }),
    );
    expect(info.targetAgentId).toBe('research_agent');
  });

  it('reads input + output previews as strings', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('refine_steer'),
          'goldfive.input_preview': attr('drift=OFF_TOPIC\ncurrent plan: ...'),
          'goldfive.output_preview': attr('new plan: research_solar_corrected'),
        },
      }),
    );
    expect(info.inputPreview).toContain('drift=OFF_TOPIC');
    expect(info.outputPreview).toContain('research_solar_corrected');
  });

  it('uses decision_summary when present and falls back otherwise', () => {
    const withSummary = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('refine_steer'),
          'goldfive.decision_summary': attr(
            'refined plan after OFF_TOPIC drift on research_solar',
          ),
        },
      }),
    );
    expect(withSummary.decisionSummary).toMatch(/refined plan/);

    const noSummary = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('refine_steer'),
        },
      }),
    );
    expect(noSummary.decisionSummary).toBe('goldfive: refine_steer');
  });
});

describe('goldfiveCallFill', () => {
  const cssVar = stubCssVar({
    '--hg-goldfive-judge-on-task': '#0af',
    '--hg-goldfive-judge-warning': '#fa0',
    '--hg-goldfive-judge-critical': '#f00',
    '--hg-goldfive-judge-neutral': '#888',
    '--hg-goldfive-refine': '#a0f',
    '--hg-goldfive-plan': '#0fa',
    '--hg-goldfive-reflective': '#ccc',
  });

  it('maps judge on_task → green (on-task var)', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('judge_reasoning'),
          'judge.on_task': boolAttr(true),
        },
      }),
    );
    expect(goldfiveCallFill(info, cssVar, '#default')).toBe('#0af');
  });

  it('maps judge critical → red (critical var)', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('judge_reasoning'),
          'judge.on_task': boolAttr(false),
          'judge.severity': attr('critical'),
        },
      }),
    );
    expect(goldfiveCallFill(info, cssVar, '#default')).toBe('#f00');
  });

  it('maps judge warning → amber (warning var)', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({
        attributes: {
          'goldfive.call_name': attr('judge_reasoning'),
          'judge.on_task': boolAttr(false),
          'judge.severity': attr('warning'),
        },
      }),
    );
    expect(goldfiveCallFill(info, cssVar, '#default')).toBe('#fa0');
  });

  it('maps refine → purple (refine var)', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({ attributes: { 'goldfive.call_name': attr('refine_steer') } }),
    );
    expect(goldfiveCallFill(info, cssVar, '#default')).toBe('#a0f');
  });

  it('maps plan (goal_derive / plan_generate) → teal', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({ attributes: { 'goldfive.call_name': attr('goal_derive') } }),
    );
    expect(goldfiveCallFill(info, cssVar, '#default')).toBe('#0fa');
  });

  it('maps reflective → grey', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({ attributes: { 'goldfive.call_name': attr('reflective_check') } }),
    );
    expect(goldfiveCallFill(info, cssVar, '#default')).toBe('#ccc');
  });

  it('returns fallback for unknown category', () => {
    const info = resolveGoldfiveSpanInfo(
      mkSpan({ name: 'novel_call', attributes: { 'goldfive.call_name': attr('novel_call') } }),
    );
    expect(goldfiveCallFill(info, cssVar, '#fallback')).toBe('#fallback');
  });

  it('returns the hard-coded hex when the CSS var is absent', () => {
    const emptyCssVar = stubCssVar();
    const info = resolveGoldfiveSpanInfo(
      mkSpan({ attributes: { 'goldfive.call_name': attr('refine_steer') } }),
    );
    // `goldfiveCallFill` returns the hex fallback hard-coded in the helper
    // (not the `fallback` arg) when the CSS var is empty — that's the
    // contract for themed colors: hex is the last-resort default.
    expect(goldfiveCallFill(info, emptyCssVar, '#default')).toBe('#a78bfa');
  });
});

describe('bareGoldfiveAgentName', () => {
  it('strips the <client>: prefix', () => {
    expect(bareGoldfiveAgentName('client-1:research_agent')).toBe('research_agent');
  });

  it('returns the input unchanged when no colon is present', () => {
    expect(bareGoldfiveAgentName('research_agent')).toBe('research_agent');
  });

  it('returns an empty string for an empty input', () => {
    expect(bareGoldfiveAgentName('')).toBe('');
  });
});

describe('truncatePreview', () => {
  it('passes through short strings unchanged', () => {
    expect(truncatePreview('hello', 10)).toBe('hello');
  });

  it('truncates with an ellipsis suffix', () => {
    expect(truncatePreview('1234567890abc', 5)).toBe('12345…');
  });

  it('returns empty string for empty / undefined input', () => {
    expect(truncatePreview('', 10)).toBe('');
  });
});
