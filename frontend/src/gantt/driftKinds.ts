// Visual metadata for plan-revision drift kinds (iter13 + iter15).
//
// The replan pipeline tags each plan revision with a drift kind string
// like ``tool_error`` or ``llm_split_task`` and packs it into
// ``plan.revisionReason`` as ``"{kind}: {detail}"``. The frontend looks
// the kind up here to render an icon, a severity color, and a short
// label so users can scan the revision history visually.

export type DriftCategory =
  | 'error'
  | 'divergence'
  | 'discovery'
  | 'user'
  | 'structural';

export interface DriftKindMeta {
  icon: string;
  color: string;
  label: string;
  category: DriftCategory;
}

export const UNKNOWN_DRIFT_KIND_META: DriftKindMeta = {
  icon: '•',
  color: '#8d9199',
  label: 'Plan revised',
  category: 'structural',
};

export const DRIFT_KIND_META: Record<string, DriftKindMeta> = {
  tool_error: { icon: '⚠', color: '#e06070', label: 'Tool error', category: 'error' },
  tool_returned_error: { icon: '🔻', color: '#e06070', label: 'Bad result', category: 'error' },
  tool_unexpected_result: { icon: '❓', color: '#f59e0b', label: 'Odd result', category: 'error' },
  task_failed: { icon: '✗', color: '#e06070', label: 'Task failed', category: 'error' },
  task_blocked: { icon: '⛔', color: '#f59e0b', label: 'Blocked', category: 'error' },
  task_empty_result: { icon: '○', color: '#8d9199', label: 'Empty result', category: 'error' },
  new_work_discovered: { icon: '✨', color: '#4caf50', label: 'New work', category: 'discovery' },
  task_result_new_work: { icon: '✨', color: '#4caf50', label: 'New work', category: 'discovery' },
  task_result_contradicts_plan: { icon: '⟷', color: '#f59e0b', label: 'Contradicts plan', category: 'divergence' },
  plan_divergence: { icon: '⟷', color: '#f59e0b', label: 'Divergence', category: 'divergence' },
  agent_reported_divergence: { icon: '⟷', color: '#f59e0b', label: 'Agent flagged divergence', category: 'divergence' },
  llm_refused: { icon: '🚫', color: '#e06070', label: 'Refused', category: 'error' },
  llm_merged_tasks: { icon: '⊕', color: '#5b8def', label: 'Merged tasks', category: 'structural' },
  llm_split_task: { icon: '⊗', color: '#5b8def', label: 'Split task', category: 'structural' },
  llm_reordered_work: { icon: '⇄', color: '#5b8def', label: 'Reordered', category: 'structural' },
  context_pressure: { icon: '⚡', color: '#f59e0b', label: 'Context limit', category: 'structural' },
  user_steer: { icon: '👆', color: '#a8c8ff', label: 'User steered', category: 'user' },
  user_cancel: { icon: '⏹', color: '#e06070', label: 'User cancelled', category: 'user' },
  unexpected_transfer: { icon: '↪', color: '#f59e0b', label: 'Unexpected transfer', category: 'divergence' },
  agent_escalated: { icon: '⚠', color: '#f59e0b', label: 'Escalated', category: 'divergence' },
  multiple_stamp_mismatches: { icon: '≠', color: '#f59e0b', label: 'Plan drift', category: 'divergence' },
  tool_call_wrong_agent: { icon: '↪', color: '#f59e0b', label: 'Wrong agent', category: 'structural' },
  transfer_to_unplanned_agent: { icon: '↪', color: '#f59e0b', label: 'Unplanned transfer', category: 'divergence' },
  failed_span: { icon: '✗', color: '#e06070', label: 'Failed span', category: 'error' },
  task_completion_out_of_order: { icon: '≠', color: '#f59e0b', label: 'Out of order', category: 'structural' },
  external_signal: { icon: '⟶', color: '#8d9199', label: 'External', category: 'user' },
  coordinator_early_stop: { icon: '⏸', color: '#f59e0b', label: 'Early stop', category: 'divergence' },
};

export interface ParsedRevisionReason {
  kind: string | null;
  detail: string;
  meta: DriftKindMeta;
}

// Parse ``"{kind}: {detail}"`` into kind + detail + lookup meta. The
// kind segment is matched against DRIFT_KIND_META — a hit returns the
// matching meta, a miss returns UNKNOWN_DRIFT_KIND_META and treats the
// whole string as the detail (so legacy revisions written before the
// kind tag was added still render cleanly).
export function parseRevisionReason(reason: string): ParsedRevisionReason {
  const text = reason || '';
  const idx = text.indexOf(': ');
  if (idx > 0) {
    const candidate = text.slice(0, idx).trim();
    const detail = text.slice(idx + 2).trim();
    const meta = DRIFT_KIND_META[candidate];
    if (meta) {
      return { kind: candidate, detail, meta };
    }
  }
  return {
    kind: null,
    detail: text.trim(),
    meta: UNKNOWN_DRIFT_KIND_META,
  };
}

export function getDriftKindMeta(kind: string | null | undefined): DriftKindMeta {
  if (!kind) return UNKNOWN_DRIFT_KIND_META;
  return DRIFT_KIND_META[kind] ?? UNKNOWN_DRIFT_KIND_META;
}
