// Rendering tests for the GoldfiveSpanDetail drawer panel (harmonograf#157).

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { GoldfiveSpanDetail } from '../../components/Interventions/GoldfiveSpanDetail';
import {
  resolveGoldfiveSpanInfo,
  type GoldfiveSpanInfo,
} from '../../lib/goldfiveSpan';
import type { AttributeValue, Span, TaskPlan } from '../../gantt/types';

function attr(value: string): AttributeValue {
  return { kind: 'string', value };
}
function intAttr(value: bigint | number): AttributeValue {
  return { kind: 'int', value: typeof value === 'bigint' ? value : BigInt(value) };
}

function mkSpan(overrides: Partial<Span> = {}): Span {
  return {
    id: 'sp-gf',
    sessionId: 'sess-1',
    agentId: 'client-1:goldfive',
    parentSpanId: null,
    kind: 'LLM_CALL',
    name: 'refine_steer',
    status: 'COMPLETED',
    startMs: 0,
    endMs: 248,
    lane: 0,
    attributes: {},
    payloadRefs: [],
    links: [],
    replaced: false,
    error: null,
    ...overrides,
  };
}

describe('<GoldfiveSpanDetail />', () => {
  it('renders the decision summary as the header title', () => {
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('refine_steer'),
        'goldfive.decision_summary': attr(
          'refined plan in response to OFF_TOPIC drift on research_solar',
        ),
      },
    });
    const info = resolveGoldfiveSpanInfo(span);
    render(<GoldfiveSpanDetail span={span} info={info} />);
    expect(
      screen.getByTestId('goldfive-span-detail-header').textContent,
    ).toMatch(/OFF_TOPIC drift on research_solar/);
  });

  it('renders call_name, target_agent, and target_task badges', () => {
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('judge_reasoning'),
        'goldfive.decision_summary': attr('reviewed plan'),
        'goldfive.target_agent_id': attr('client-1:research_agent'),
        'goldfive.target_task_id': attr('t-research'),
      },
    });
    const info = resolveGoldfiveSpanInfo(span);
    render(<GoldfiveSpanDetail span={span} info={info} />);
    expect(screen.getByTestId('goldfive-span-detail-call-name').textContent).toBe(
      'judge_reasoning',
    );
    expect(
      screen.getByTestId('goldfive-span-detail-target-agent').textContent,
    ).toMatch(/research_agent/);
    expect(
      screen.getByTestId('goldfive-span-detail-target-task').textContent,
    ).toMatch(/t-research/);
  });

  it('renders the input + output preview blocks verbatim', () => {
    const input = 'drift=OFF_TOPIC\nprevious plan summary';
    const output = 'new plan: research_solar_corrected';
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('refine_steer'),
        'goldfive.input_preview': attr(input),
        'goldfive.output_preview': attr(output),
      },
    });
    const info = resolveGoldfiveSpanInfo(span);
    render(<GoldfiveSpanDetail span={span} info={info} />);
    expect(
      screen.getByTestId('goldfive-span-detail-input-body').textContent,
    ).toBe(input);
    expect(
      screen.getByTestId('goldfive-span-detail-output-body').textContent,
    ).toBe(output);
  });

  it('renders "No preview captured" when input/output are absent', () => {
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('refine_steer'),
      },
    });
    const info = resolveGoldfiveSpanInfo(span);
    render(<GoldfiveSpanDetail span={span} info={info} />);
    expect(
      screen.getByTestId('goldfive-span-detail-input').textContent,
    ).toMatch(/No input preview captured/);
    expect(
      screen.getByTestId('goldfive-span-detail-output').textContent,
    ).toMatch(/No output preview captured/);
  });

  it('fires the copy button for input + output', () => {
    const onCopy = vi.fn();
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('refine_steer'),
        'goldfive.input_preview': attr('IN'),
        'goldfive.output_preview': attr('OUT'),
      },
    });
    const info = resolveGoldfiveSpanInfo(span);
    render(
      <GoldfiveSpanDetail span={span} info={info} onCopy={onCopy} />,
    );
    fireEvent.click(screen.getByTestId('goldfive-span-detail-input-copy'));
    fireEvent.click(screen.getByTestId('goldfive-span-detail-output-copy'));
    expect(onCopy).toHaveBeenCalledTimes(2);
    expect(onCopy).toHaveBeenCalledWith('IN');
    expect(onCopy).toHaveBeenCalledWith('OUT');
  });

  it('renders the context definition list with model, elapsed, run, session, task, target', () => {
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('judge_reasoning'),
        'goldfive.target_agent_id': attr('client-1:research_agent'),
        'goldfive.target_task_id': attr('t-research'),
        'goldfive.model': attr('claude-haiku-4'),
        'goldfive.elapsed_ms': intAttr(248),
        'goldfive.run_id': attr('run-alpha'),
      },
    });
    const info = resolveGoldfiveSpanInfo(span);
    render(<GoldfiveSpanDetail span={span} info={info} />);
    expect(screen.getByTestId('goldfive-span-detail-ctx-model').textContent).toBe(
      'claude-haiku-4',
    );
    expect(
      screen.getByTestId('goldfive-span-detail-ctx-elapsed').textContent,
    ).toBe('248ms');
    expect(screen.getByTestId('goldfive-span-detail-ctx-run').textContent).toBe(
      'run-alpha',
    );
    expect(
      screen.getByTestId('goldfive-span-detail-ctx-session').textContent,
    ).toBe('sess-1');
    expect(screen.getByTestId('goldfive-span-detail-ctx-task').textContent).toBe(
      't-research',
    );
    expect(
      screen.getByTestId('goldfive-span-detail-ctx-target').textContent,
    ).toBe('client-1:research_agent');
  });

  it('falls back to span duration for elapsed when no attribute is set', () => {
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('refine_steer'),
      },
      startMs: 1_000,
      endMs: 1_500,
    });
    const info = resolveGoldfiveSpanInfo(span);
    render(<GoldfiveSpanDetail span={span} info={info} />);
    expect(
      screen.getByTestId('goldfive-span-detail-ctx-elapsed').textContent,
    ).toBe('500ms');
  });

  it('renders the linked-plan section for refine_* calls when a plan is provided', () => {
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('refine_steer'),
        'goldfive.target_task_id': attr('t-research'),
      },
    });
    const info = resolveGoldfiveSpanInfo(span);
    const plan: TaskPlan = {
      id: 'p1',
      invocationSpanId: '',
      plannerAgentId: '',
      createdAtMs: 300,
      summary: '',
      tasks: [
        {
          id: 't-research-corrected',
          title: 'Research corrected topic',
          description: '',
          assigneeAgentId: 'client-1:research_agent',
          status: 'PENDING',
          predictedStartMs: 0,
          predictedDurationMs: 0,
          boundSpanId: '',
        },
      ],
      edges: [],
      revisionReason: 'OFF_TOPIC drift',
      revisionIndex: 2,
    };
    const onOpen = vi.fn();
    render(
      <GoldfiveSpanDetail
        span={span}
        info={info}
        linkedPlanRevision={{ plan, onOpen }}
      />,
    );
    const link = screen.getByTestId('goldfive-span-detail-linked-plan-link');
    expect(link.textContent).toMatch(/Plan refined → r2/);
    expect(link.textContent).toMatch(/OFF_TOPIC drift/);
    fireEvent.click(link);
    expect(onOpen).toHaveBeenCalledTimes(1);
    const tasksList = screen.getByTestId(
      'goldfive-span-detail-linked-plan-tasks',
    );
    expect(tasksList.textContent).toMatch(/Research corrected topic/);
  });

  it('does not render the linked-plan section for non-refine categories', () => {
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('judge_reasoning'),
      },
    });
    const info = resolveGoldfiveSpanInfo(span);
    const plan: TaskPlan = {
      id: 'p1',
      invocationSpanId: '',
      plannerAgentId: '',
      createdAtMs: 300,
      summary: '',
      tasks: [],
      edges: [],
      revisionReason: 'x',
      revisionIndex: 1,
    };
    render(
      <GoldfiveSpanDetail
        span={span}
        info={info}
        linkedPlanRevision={{ plan }}
      />,
    );
    expect(screen.queryByTestId('goldfive-span-detail-linked-plan')).toBeNull();
  });

  it('renders the decision summary fallback when only the call_name is stamped', () => {
    const span = mkSpan({
      attributes: {
        'goldfive.call_name': attr('goal_derive'),
      },
    });
    const info: GoldfiveSpanInfo = resolveGoldfiveSpanInfo(span);
    render(<GoldfiveSpanDetail span={span} info={info} />);
    expect(screen.getByTestId('goldfive-span-detail-header').textContent).toMatch(
      /goldfive: goal_derive/,
    );
  });
});
