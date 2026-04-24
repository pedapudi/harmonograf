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
    targetAgentId: 'client:agent-a',
    taskId: 't1',
    verdictBucket: 'on_task',
    verdictTone: 'on_task',
    onTask: true,
    severity: '',
    reason: '',
    reasoningInput: '',
    rawResponse: '',
    parseSuccessful: true,
    inputPreview: '',
    outputPreview: '',
    decisionSummary: '',
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

  it('expands the reasoning-input section by default and collapses on click', () => {
    // Drawer variant: reasoning-input is the primary context, rendered
    // expanded by default so the operator doesn't have to click to see
    // what the judge was evaluating.
    render(
      <JudgeInvocationDetail
        detail={detail({
          reasoningInput:
            'The agent said: "I will now search for widgets, then rank them by size."',
        })}
      />,
    );
    const reasoning = screen.getByTestId('judge-detail-reasoning');
    expect(reasoning.getAttribute('data-open')).toBe('true');
    expect(screen.getByTestId('judge-detail-reasoning-body').textContent)
      .toContain('rank them by size');
    fireEvent.click(screen.getByTestId('judge-detail-reasoning-toggle'));
    expect(
      screen.getByTestId('judge-detail-reasoning').getAttribute('data-open'),
    ).toBe('false');
    expect(screen.queryByTestId('judge-detail-reasoning-body')).toBeNull();
  });

  it('expands the raw-response section by default and collapses on click', () => {
    // Drawer variant: raw-response is expanded by default so operators
    // diagnosing a malformed verdict see the LLM's output without extra
    // clicks.
    render(
      <JudgeInvocationDetail
        detail={detail({ rawResponse: '{"on_task": false, "severity": "warning"}' })}
      />,
    );
    const raw = screen.getByTestId('judge-detail-raw');
    expect(raw.getAttribute('data-open')).toBe('true');
    expect(screen.getByTestId('judge-detail-raw-body').textContent).toContain(
      '"severity": "warning"',
    );
    fireEvent.click(screen.getByTestId('judge-detail-raw-toggle'));
    expect(screen.queryByTestId('judge-detail-raw-body')).toBeNull();
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
    expect(link.textContent).toMatch(/refined the plan/i);
    expect(link.textContent).toMatch(/r3/);
    fireEvent.click(link);
    expect(onOpenSteering).toHaveBeenCalledWith('plan-x', 3);
    expect(screen.getByTestId('judge-detail-steering-tasks').textContent)
      .toContain('verify widget count');
    expect(screen.getByTestId('judge-detail-steering-tasks').textContent)
      .toContain('cancelled: old widget step');
  });

  // -----------------------------------------------------------------
  // Drawer variant — Section A / B / C coverage.
  // -----------------------------------------------------------------

  describe('drawer variant — Section A (what was being judged)', () => {
    it('renders the reasoning_input text in full (no truncation)', () => {
      const long =
        'Step 1 — search widgets. Step 2 — filter by size. '.repeat(40);
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail({ reasoningInput: long })}
        />,
      );
      const body = screen.getByTestId('judge-detail-reasoning-body');
      expect(body.textContent).toBe(long);
    });

    it('renders taskTitle + taskDescription when supplied', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail()}
          taskTitle="Plan weekend trip"
          taskDescription="Draft a 3-day itinerary for Big Sur."
        />,
      );
      const card = screen.getByTestId('judge-drawer-task-context');
      expect(card.textContent).toContain('Plan weekend trip');
      expect(card.textContent).toContain('Big Sur');
    });

    it('renders goals as a bulleted list when goals are present', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail()}
          goals={['Book lodging', 'Plan hikes']}
        />,
      );
      const goalsBlock = screen.getByTestId('judge-drawer-goals');
      expect(goalsBlock).toBeTruthy();
      const items = screen.getAllByTestId('judge-drawer-goal');
      expect(items.length).toBe(2);
      expect(items[0].textContent).toContain('Book lodging');
      expect(items[1].textContent).toContain('Plan hikes');
    });

    it('omits the goals section when no goals are supplied', () => {
      render(
        <JudgeInvocationDetail variant="drawer" detail={detail()} />,
      );
      expect(screen.queryByTestId('judge-drawer-goals')).toBeNull();
    });

    it('omits the goals section when all goals are empty strings', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail()}
          goals={['', '   ']}
        />,
      );
      expect(screen.queryByTestId('judge-drawer-goals')).toBeNull();
    });

    it('falls back to "Reasoning input not recorded" when judge.reasoning_input is empty', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail({ reasoningInput: '' })}
        />,
      );
      expect(screen.getByTestId('judge-detail-reasoning-empty').textContent)
        .toMatch(/not recorded/i);
    });

    it('falls back to "Unknown agent" when subjectAgentId is empty', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail({ subjectAgentId: '', targetAgentId: '' })}
        />,
      );
      expect(screen.getByTestId('judge-detail-agent').textContent)
        .toMatch(/Unknown agent/i);
    });
  });

  describe('drawer variant — Section B (what the judgement is)', () => {
    it('renders the raw response in full when rawResponse is set', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail({
            verdictBucket: 'off_task',
            verdictTone: 'off_task_warning',
            severity: 'warning',
            reason: 'Drifting toward unrelated topic.',
            rawResponse: '{"on_task": false, "severity": "warning"}',
          })}
        />,
      );
      expect(screen.getByTestId('judge-detail-raw-body').textContent).toBe(
        '{"on_task": false, "severity": "warning"}',
      );
    });

    it('falls back to "Raw response not recorded" when rawResponse is empty', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail({
            verdictBucket: 'off_task',
            verdictTone: 'off_task_warning',
            severity: 'warning',
            reason: 'whoops',
          })}
        />,
      );
      expect(screen.getByTestId('judge-detail-raw-empty').textContent)
        .toMatch(/not recorded/i);
    });

    it('renders parsed on_task + severity when parseSuccessful=true', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail({
            verdictBucket: 'off_task',
            verdictTone: 'off_task_info',
            parseSuccessful: true,
            onTask: false,
            severity: 'info',
          })}
        />,
      );
      expect(
        screen.getByTestId('judge-drawer-parsed-on-task').textContent,
      ).toBe('false');
      expect(
        screen.getByTestId('judge-drawer-parsed-severity').textContent,
      ).toBe('info');
      const diag = screen.getByTestId('judge-drawer-parse-diagnostic');
      expect(diag.getAttribute('data-ok')).toBe('true');
      expect(diag.textContent).toMatch(/successfully parsed/i);
    });

    it('shows the malformed-response diagnostic when parseSuccessful=false', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail({
            verdictBucket: 'no_verdict',
            verdictTone: 'no_verdict',
            parseSuccessful: false,
            onTask: false,
            severity: '',
            rawResponse: 'not json',
          })}
        />,
      );
      const diag = screen.getByTestId('judge-drawer-parse-diagnostic');
      expect(diag.getAttribute('data-ok')).toBe('false');
      expect(diag.textContent).toMatch(/malformed/i);
      // on_task dash when parse failed.
      expect(
        screen.getByTestId('judge-drawer-parsed-on-task').textContent,
      ).toBe('—');
    });
  });

  describe('drawer variant — Section C (steering outcome)', () => {
    it('renders the steering link and decision summary when a PlanRevised matched', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail({
            verdictBucket: 'off_task',
            verdictTone: 'off_task_warning',
            onTask: false,
            severity: 'warning',
            steeredPlan: {
              id: 'plan-a',
              invocationSpanId: '',
              plannerAgentId: '',
              createdAtMs: 200,
              summary: '',
              tasks: [],
              edges: [],
              revisionReason: 'verify outputs',
              revisionKind: 'reasoning_drift',
              revisionSeverity: 'warning',
              revisionIndex: 2,
              triggerEventId: 'ev-1',
            },
            steeringSummary: 'verify outputs',
            decisionSummary: 'Inserted a verification step after step 3.',
          })}
          onOpenSteering={() => {}}
        />,
      );
      expect(screen.getByTestId('judge-detail-steering-link').textContent)
        .toMatch(/refined the plan/i);
      expect(
        screen.getByTestId('judge-drawer-decision-summary').textContent,
      ).toContain('verification step');
    });

    it('does not render the steering section for on_task verdicts', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail({ verdictBucket: 'on_task' })}
        />,
      );
      expect(screen.queryByTestId('judge-detail-steering')).toBeNull();
    });
  });

  describe('drawer variant — graceful degradation', () => {
    it('renders without crashing when all optional attributes are missing', () => {
      render(
        <JudgeInvocationDetail
          variant="drawer"
          detail={detail({
            subjectAgentId: '',
            targetAgentId: '',
            taskId: '',
            model: '',
            reason: '',
            reasoningInput: '',
            rawResponse: '',
            severity: '',
            verdictBucket: 'no_verdict',
            verdictTone: 'no_verdict',
            onTask: false,
            parseSuccessful: false,
            elapsedMs: 0,
          })}
        />,
      );
      expect(screen.getByTestId('judge-invocation-detail')).toBeTruthy();
      expect(screen.getByTestId('judge-detail-reasoning-empty')).toBeTruthy();
      expect(screen.getByTestId('judge-detail-raw-empty')).toBeTruthy();
    });
  });

  // -----------------------------------------------------------------
  // Popover variant — banner + context row + input preview.
  // -----------------------------------------------------------------

  describe('popover variant', () => {
    it('renders a green "On task" banner for on_task verdicts', () => {
      render(
        <JudgeInvocationDetail
          variant="popover"
          detail={detail({ verdictBucket: 'on_task', verdictTone: 'on_task' })}
        />,
      );
      const banner = screen.getByTestId('judge-popover-banner');
      expect(banner.getAttribute('data-tone')).toBe('on_task');
      expect(banner.textContent).toMatch(/On task/i);
    });

    it('renders an amber "Off task (warning)" banner for off_task warning', () => {
      render(
        <JudgeInvocationDetail
          variant="popover"
          detail={detail({
            verdictBucket: 'off_task',
            verdictTone: 'off_task_warning',
            severity: 'warning',
            reason: 'paraphrasing',
          })}
        />,
      );
      const banner = screen.getByTestId('judge-popover-banner');
      expect(banner.getAttribute('data-tone')).toBe('off_task_warning');
      expect(banner.textContent).toMatch(/Off task.*warning/i);
    });

    it('renders a red "Off task (critical)" banner for off_task critical', () => {
      render(
        <JudgeInvocationDetail
          variant="popover"
          detail={detail({
            verdictBucket: 'off_task',
            verdictTone: 'off_task_critical',
            severity: 'critical',
            reason: 'invented tool output',
          })}
        />,
      );
      const banner = screen.getByTestId('judge-popover-banner');
      expect(banner.getAttribute('data-tone')).toBe('off_task_critical');
      expect(banner.textContent).toMatch(/Off task.*critical/i);
    });

    it('renders a grey "No verdict" banner for no_verdict buckets', () => {
      render(
        <JudgeInvocationDetail
          variant="popover"
          detail={detail({
            verdictBucket: 'no_verdict',
            verdictTone: 'no_verdict',
            rawResponse: '{"bad_json":',
          })}
        />,
      );
      const banner = screen.getByTestId('judge-popover-banner');
      expect(banner.getAttribute('data-tone')).toBe('no_verdict');
      expect(banner.textContent).toMatch(/No verdict/i);
    });

    it('renders the lead reason just below the banner', () => {
      render(
        <JudgeInvocationDetail
          variant="popover"
          detail={detail({
            verdictBucket: 'off_task',
            verdictTone: 'off_task_warning',
            reason: 'Agent is summarising instead of citing tool output.',
          })}
        />,
      );
      expect(screen.getByTestId('judge-popover-lead').textContent).toContain(
        'summarising',
      );
    });

    it('falls through to the first 140 chars of rawResponse when reason is empty', () => {
      const raw = 'X'.repeat(300);
      render(
        <JudgeInvocationDetail
          variant="popover"
          detail={detail({
            verdictBucket: 'no_verdict',
            verdictTone: 'no_verdict',
            reason: '',
            rawResponse: raw,
          })}
        />,
      );
      expect(
        screen.getByTestId('judge-popover-lead').textContent?.length,
      ).toBeLessThanOrEqual(140);
    });

    it('renders the context row with subject, task, model, and elapsed', () => {
      render(
        <JudgeInvocationDetail
          variant="popover"
          detail={detail({
            subjectAgentId: 'client:agent-zeta',
            taskId: 'task-99',
            model: 'haiku-4',
            elapsedMs: 315,
          })}
        />,
      );
      expect(
        screen.getByTestId('judge-popover-subject').textContent,
      ).toContain('agent-zeta');
      expect(screen.getByTestId('judge-popover-task').textContent).toContain(
        'task-99',
      );
      expect(screen.getByTestId('judge-popover-model').textContent).toContain(
        'haiku-4',
      );
      expect(
        screen.getByTestId('judge-popover-elapsed').textContent,
      ).toContain('315ms');
    });

    it('renders the input-preview section collapsed by default with a drawer hint', () => {
      render(
        <JudgeInvocationDetail
          variant="popover"
          detail={detail({
            reasoningInput: 'A'.repeat(500),
          })}
        />,
      );
      const preview = screen.getByTestId('judge-popover-input');
      expect(preview.getAttribute('data-open')).toBe('false');
      // Body not in the DOM until expanded.
      expect(screen.queryByTestId('judge-popover-input-body')).toBeNull();
      fireEvent.click(screen.getByTestId('judge-popover-input-toggle'));
      const body = screen.getByTestId('judge-popover-input-body');
      // Truncated to POPOVER_PREVIEW_CHARS (200) + trailing ellipsis.
      expect(body.textContent?.endsWith('…')).toBe(true);
      expect((body.textContent ?? '').length).toBeLessThanOrEqual(205);
      expect(
        screen.getByTestId('judge-popover-drawer-hint').textContent,
      ).toMatch(/drawer/i);
    });

    it('prefers goldfive.input_preview over judge.reasoning_input for the popover preview', () => {
      render(
        <JudgeInvocationDetail
          variant="popover"
          detail={detail({
            reasoningInput: 'LONG UNTRIMMED TEXT',
            inputPreview: 'short preview',
          })}
        />,
      );
      fireEvent.click(screen.getByTestId('judge-popover-input-toggle'));
      expect(
        screen.getByTestId('judge-popover-input-body').textContent,
      ).toBe('short preview');
    });

    it('shows "Reasoning input not recorded" placeholder when no input is available', () => {
      render(
        <JudgeInvocationDetail
          variant="popover"
          detail={detail({ reasoningInput: '', inputPreview: '' })}
        />,
      );
      fireEvent.click(screen.getByTestId('judge-popover-input-toggle'));
      expect(
        screen.getByTestId('judge-popover-input-empty').textContent,
      ).toMatch(/not recorded/i);
    });
  });
});
