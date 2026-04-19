import { useMemo, useState } from 'react';
import { useApprovalsStore, type PendingApproval } from '../../state/approvalsStore';
import { useUiStore } from '../../state/uiStore';
import { useSendControl } from '../../rpc/hooks';

// Stable sentinel so the zustand selector below returns a referentially
// stable value when the session has no pending approvals — otherwise
// useSyncExternalStore tears on every render.
const EMPTY_APPROVALS: PendingApproval[] = [];

// Surfaces goldfive ApprovalRequested events queued on the approvalsStore.
//
// One entry per pending approval; entries are removed client-side when the
// matching ApprovalGranted / ApprovalRejected arrives, so a submit-then-
// acknowledge round-trip auto-dismisses without an explicit close.
//
// Two flows share this surface:
//   * Flow A (task-level): `kind` = "task", `target_id` = task id, metadata
//     carries whatever the reporting tool chose to pack.
//   * Flow B (tool-level): `kind` = "tool", `target_id` = ADK function-call
//     id, metadata carries "tool_name" and "args_json".
// The drawer renders Flow B with the tool/name args preview card, Flow A with
// the prompt alone.

export function ApprovalDrawer() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const approvals = useApprovalsStore((s) =>
    sessionId ? s.bySession.get(sessionId) ?? EMPTY_APPROVALS : EMPTY_APPROVALS,
  );
  if (!sessionId || approvals.length === 0) return null;
  return (
    <aside
      className="hg-approval-drawer"
      data-testid="approval-drawer"
      role="region"
      aria-label="Pending approvals"
      style={{
        position: 'fixed',
        top: 72,
        right: 16,
        width: 380,
        maxHeight: '70vh',
        overflowY: 'auto',
        zIndex: 40,
        background: 'var(--md-sys-color-surface-container-high, #232832)',
        color: 'var(--md-sys-color-on-surface, #e2e2e9)',
        border: '1px solid var(--md-sys-color-outline-variant, #43474e)',
        borderRadius: 12,
        boxShadow: '0 8px 28px rgba(0,0,0,0.5)',
        padding: 8,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div
        style={{
          padding: '4px 8px',
          fontSize: 11,
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
          opacity: 0.7,
        }}
      >
        Pending approvals · {approvals.length}
      </div>
      {approvals.map((a) => (
        <ApprovalCard key={`${a.sessionId}:${a.targetId}`} approval={a} />
      ))}
    </aside>
  );
}

function ApprovalCard({ approval }: { approval: PendingApproval }) {
  const send = useSendControl();
  const [busy, setBusy] = useState<'APPROVE' | 'REJECT' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const toolName = approval.metadata['tool_name'] ?? '';
  const argsJson = approval.metadata['args_json'] ?? '';

  const prettyArgs = useMemo(() => {
    if (!argsJson) return '';
    try {
      return JSON.stringify(JSON.parse(argsJson), null, 2);
    } catch {
      return argsJson;
    }
  }, [argsJson]);

  const dispatch = async (kind: 'APPROVE' | 'REJECT') => {
    setBusy(kind);
    setError(null);
    try {
      // Encode target_id in the payload body so the server-side
      // ControlChannel bridge can quote it back onto the
      // pending-approval waiter regardless of which agent/span we
      // happened to observe.
      const payload = new TextEncoder().encode(
        JSON.stringify({ target_id: approval.targetId }),
      );
      await send({
        sessionId: approval.sessionId,
        agentId: approval.agentId,
        spanId: approval.spanId,
        kind,
        payload,
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div
      data-testid="approval-card"
      data-target-id={approval.targetId}
      data-kind={approval.kind}
      style={{
        padding: 10,
        borderRadius: 8,
        background: 'var(--md-sys-color-error-container, #5c1a1b)',
        color: 'var(--md-sys-color-on-error-container, #ffdad6)',
        border: '1px solid var(--md-sys-color-error, #ffb4ab)',
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        fontSize: 12,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <span style={{ fontWeight: 700 }}>Approval required</span>
        <span
          style={{
            fontSize: 10,
            padding: '1px 6px',
            borderRadius: 8,
            border: '1px solid currentColor',
            textTransform: 'uppercase',
            letterSpacing: '0.04em',
            opacity: 0.85,
          }}
        >
          {approval.kind || 'task'}
        </span>
        {toolName && (
          <code
            data-testid="approval-tool-name"
            style={{ fontSize: 11, opacity: 0.9 }}
          >
            {toolName}
          </code>
        )}
      </div>
      <div
        data-testid="approval-prompt"
        style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}
      >
        {approval.prompt || '(no prompt)'}
      </div>
      {prettyArgs && (
        <pre
          data-testid="approval-args"
          style={{
            margin: 0,
            padding: 6,
            background: 'rgba(0,0,0,0.3)',
            borderRadius: 6,
            maxHeight: 160,
            overflow: 'auto',
            fontSize: 11,
            fontFamily: "ui-monospace, 'SF Mono', Consolas, monospace",
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {prettyArgs}
        </pre>
      )}
      {approval.taskId && approval.taskId !== approval.targetId && (
        <div style={{ fontSize: 10, opacity: 0.75 }}>
          task <code>{approval.taskId}</code>
        </div>
      )}
      {error && (
        <div
          data-testid="approval-error"
          style={{ fontSize: 11, opacity: 0.9 }}
        >
          {error}
        </div>
      )}
      <div
        style={{
          display: 'flex',
          gap: 6,
          justifyContent: 'flex-end',
          paddingTop: 2,
        }}
      >
        <button
          data-testid="approval-reject"
          onClick={() => void dispatch('REJECT')}
          disabled={busy !== null}
        >
          {busy === 'REJECT' ? 'Rejecting…' : 'Reject'}
        </button>
        <button
          data-testid="approval-approve"
          onClick={() => void dispatch('APPROVE')}
          disabled={busy !== null}
        >
          {busy === 'APPROVE' ? 'Approving…' : 'Approve'}
        </button>
      </div>
    </div>
  );
}
