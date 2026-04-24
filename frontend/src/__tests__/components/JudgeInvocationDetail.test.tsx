// harmonograf#197 — rendering tests for JudgeInvocationDetail.
//
// Covers the five verdict buckets the component supports and the copy /
// collapse affordances. The source-of-truth for what data flows in is
// lib/interventionDetail.resolveJudgeDetail; these tests feed that shape
// directly so renderer + resolver are testable in isolation.

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { JudgeInvocationDetail } from '../../components/Interventions/JudgeInvocationDetail';
import type { JudgeDetail } from '../../lib/interventionDetail';

vi.mock('../../components/Interventions/JudgeInvocationDetail.css', () => ({}));

function detail(over: Partial<JudgeDetail> = {}): JudgeDetail {
  return {
    spanId: 'judge-1',
    eventId: 'ev-1',
    recordedAtMs: 12_000,
    model: 'claude-haiku-4',
    elapsedMs: 248,
    subjectAgentId: 'client:agent-a',
    taskId: 't1',
    verdictBucket: 'on_task',
    onTask: true,
    severity: '',
    reason: '',
    reasoningInput: '',
    rawResponse: '',
    steeredPlan: null,
    steeringSummary: '',
    taskSummaries: [],
    ...over,
  };
}

describe('<JudgeInvocationDetail />', () => {
  it('renders the on-task verdict with a green badge and no severity pill', () => {
    render(
      <JudgeInvocationDetail
        detail={detail({
          verdictBucket: 'on_task',
          onTask: true,
          reason: 'The agent is making expected progress.',
        })}
      />,
    );
    const verdict = screen.getByTestId('judge-detail-verdict');
    expect(verdict.textContent).toMatch(/On task/i);
    expect(verdict.getAttribute('data-bucket')).toBe('on_task');
    expect(screen.queryByTestId('judge-detail-severity')).toBeNull();
    // on_task reason renders inline (no off-task border).
    expect(screen.getByText(/making expected progress/i)).toBeTruthy();
  });

  it('renders the off-task INFO verdict with severity pill', () => {
    render(
      <JudgeInvocationDetail
        detail={detail({
          verdictBucket: 'off_task',
          onTask: false,
          severity: 'info',
          reason: 'Mild topic drift.',
        })}
      />,
    );
    const sev = screen.getByTestId('judge-detail-severity');
    expect(sev.getAttribute('data-severity')).toBe('info');
    expect(screen.getByText(/Mild topic drift/i)).toBeTruthy();
  });

  it('renders the off-task WARNING verdict with severity pill', () => {
    render(
      <JudgeInvocationDetail
        detail={detail({
          verdictBucket: 'off_task',
          onTask: false,
          severity: 'warning',
          reason: 'Agent is paraphrasing.',
        })}
      />,
    );
    expect(
      screen.getByTestId('judge-detail-severity').getAttribute('data-severity'),
    ).toBe('warning');
  });

  it('renders the off-task CRITICAL verdict with severity pill', () => {
    render(
      <JudgeInvocationDetail
        detail={detail({
          verdictBucket: 'off_task',
          onTask: false,
          severity: 'critical',
          reason: 'Agent invented tool output.',
        })}
      />,
    );
    expect(
      screen.getByTestId('judge-detail-severity').getAttribute('data-severity'),
    ).toBe('critical');
    // Critical off-task reason rendered in the off-task reason block.
    expect(screen.getByText(/invented tool output/i)).toBeTruthy();
  });

  it('renders the "no verdict" bucket with the raw-response hint', () => {
    render(
      <JudgeInvocationDetail
        detail={detail({
          verdictBucket: 'no_verdict',
          onTask: false,
          severity: '',
          reason: '',
          rawResponse: '{"bad_json":',
        })}
      />,
    );
    const verdict = screen.getByTestId('judge-detail-verdict');
    expect(verdict.getAttribute('data-bucket')).toBe('no_verdict');
    expect(screen.getByTestId('judge-detail-no-verdict')).toBeTruthy();
    // Severity pill hidden for no_verdict.
    expect(screen.queryByTestId('judge-detail-severity')).toBeNull();
  });

  it('renders the header meta (time, elapsed ms, model)', () => {
    render(
      <JudgeInvocationDetail
        detail={detail({ recordedAtMs: 125_000, elapsedMs: 410, model: 'haiku' })}
      />,
    );
    expect(screen.getByText('2:05')).toBeTruthy();
    expect(screen.getByText('410ms')).toBeTruthy();
    expect(screen.getByText('haiku')).toBeTruthy();
  });

  it('collapses the reasoning-input section by default and expands on click', () => {
    render(
      <JudgeInvocationDetail
        detail={detail({
          reasoningInput:
            'The agent said: "I will now search for widgets, then rank them by size."',
        })}
      />,
    );
    const reasoning = screen.getByTestId('judge-detail-reasoning');
    expect(reasoning.getAttribute('data-open')).toBe('false');
    expect(screen.queryByTestId('judge-detail-reasoning-body')).toBeNull();
    fireEvent.click(screen.getByTestId('judge-detail-reasoning-toggle'));
    expect(
      screen.getByTestId('judge-detail-reasoning').getAttribute('data-open'),
    ).toBe('true');
    expect(screen.getByTestId('judge-detail-reasoning-body').textContent)
      .toContain('rank them by size');
  });

  it('collapses the raw-response section by default and expands on click', () => {
    render(
      <JudgeInvocationDetail
        detail={detail({ rawResponse: '{"on_task": false, "severity": "warning"}' })}
      />,
    );
    const raw = screen.getByTestId('judge-detail-raw');
    expect(raw.getAttribute('data-open')).toBe('false');
    expect(screen.queryByTestId('judge-detail-raw-body')).toBeNull();
    fireEvent.click(screen.getByTestId('judge-detail-raw-toggle'));
    expect(screen.getByTestId('judge-detail-raw-body').textContent).toContain(
      '"severity": "warning"',
    );
  });

  it('wires the copy-to-clipboard button', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      writable: true,
      configurable: true,
    });
    render(
      <JudgeInvocationDetail
        detail={detail({ reasoningInput: 'hello from the agent' })}
      />,
    );
    fireEvent.click(screen.getByTestId('judge-detail-reasoning-copy'));
    expect(writeText).toHaveBeenCalledWith('hello from the agent');
  });

  it('renders the context section with agent + task links when handlers supplied', () => {
    const onFocusAgent = vi.fn();
    const onFocusTask = vi.fn();
    render(
      <JudgeInvocationDetail
        detail={detail({ subjectAgentId: 'client:agent-a', taskId: 'task-42' })}
        onFocusAgent={onFocusAgent}
        onFocusTask={onFocusTask}
      />,
    );
    fireEvent.click(screen.getByTestId('judge-detail-agent'));
    fireEvent.click(screen.getByTestId('judge-detail-task'));
    expect(onFocusAgent).toHaveBeenCalledWith('client:agent-a');
    expect(onFocusTask).toHaveBeenCalledWith('task-42');
  });

  it('renders the steering section only for off-task verdicts', () => {
    const { rerender } = render(
      <JudgeInvocationDetail detail={detail({ verdictBucket: 'on_task' })} />,
    );
    expect(screen.queryByTestId('judge-detail-steering')).toBeNull();
    rerender(
      <JudgeInvocationDetail
        detail={detail({
          verdictBucket: 'off_task',
          severity: 'warning',
          reason: 'bad',
        })}
      />,
    );
    expect(screen.getByTestId('judge-detail-steering')).toBeTruthy();
    // No matching PlanRevised → the "No steering applied" hint shows.
    expect(screen.getByTestId('judge-detail-no-steering')).toBeTruthy();
  });

  it('shows the "refined plan" link + task summaries when a PlanRevised matched', () => {
    const onOpenSteering = vi.fn();
    render(
      <JudgeInvocationDetail
        detail={detail({
          verdictBucket: 'off_task',
          onTask: false,
          severity: 'warning',
          reason: 'drifting',
          steeredPlan: {
            id: 'plan-x',
            invocationSpanId: '',
            plannerAgentId: '',
            createdAtMs: 500,
            summary: '',
            tasks: [],
            edges: [],
            revisionReason: 'add verification step',
            revisionKind: 'looping_reasoning',
            revisionSeverity: 'warning',
            revisionIndex: 3,
            triggerEventId: 'ev-1',
          },
          steeringSummary: 'add verification step',
          taskSummaries: ['verify widget count', 'cancelled: old widget step'],
        })}
        onOpenSteering={onOpenSteering}
      />,
    );
    const link = screen.getByTestId('judge-detail-steering-link');
    expect(link.textContent).toMatch(/refined plan/i);
    expect(link.textContent).toMatch(/r3/);
    fireEvent.click(link);
    expect(onOpenSteering).toHaveBeenCalledWith('plan-x', 3);
    expect(screen.getByTestId('judge-detail-steering-tasks').textContent)
      .toContain('verify widget count');
    expect(screen.getByTestId('judge-detail-steering-tasks').textContent)
      .toContain('cancelled: old widget step');
  });
});
