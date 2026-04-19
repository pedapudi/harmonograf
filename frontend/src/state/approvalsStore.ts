import { create } from 'zustand';

// Per-session HITL approval queue fed by goldfive ApprovalRequested events.
//
// ApprovalRequested adds a pending entry keyed by target_id; the matching
// ApprovalGranted / ApprovalRejected (same target_id) clears it. target_id is
// a task id for Flow A (report_awaiting_approval) or an ADK function-call id
// for Flow B (before_tool_callback) — the kind string tells the UI which.
//
// Entries carry the `agentId` observed on the most recent goldfive span for
// the same target so the UI's Approve/Reject click can route the
// ControlEvent to the exact agent (ControlRouter target = agentId + spanId).
// If we haven't seen such a span, agentId stays empty; SendControl with an
// empty agentId still reaches the session-level ControlChannel bridge.

export interface PendingApproval {
  sessionId: string;
  targetId: string;
  kind: string;
  prompt: string;
  taskId: string;
  metadata: Record<string, string>;
  // Session-relative ms when the ApprovalRequested event was observed, so
  // the UI can show a "waiting for Xs" timer without a separate wall clock.
  requestedAtMs: number;
  // Agent id to quote back on APPROVE/REJECT. Derived later from the span
  // context in the caller when known; empty string is valid (session-level).
  agentId: string;
  // Optional span id for the approval anchor (same derivation).
  spanId: string;
}

interface ApprovalsState {
  bySession: Map<string, PendingApproval[]>;
  list(sessionId: string | null): PendingApproval[];
  // Push a new ApprovalRequested, or replace an existing entry with the same
  // targetId (server replay during reconnect may resend the same request).
  request(entry: PendingApproval): void;
  // Dismiss by targetId. Called on ApprovalGranted / ApprovalRejected.
  resolve(sessionId: string, targetId: string): void;
  // Clear all pending approvals for a session (e.g. on session switch).
  clear(sessionId: string): void;
}

export const useApprovalsStore = create<ApprovalsState>((set, get) => ({
  bySession: new Map(),

  list(sessionId) {
    if (!sessionId) return [];
    return get().bySession.get(sessionId) ?? [];
  },

  request(entry) {
    set((state) => {
      const next = new Map(state.bySession);
      const arr = (next.get(entry.sessionId) ?? []).slice();
      const idx = arr.findIndex((a) => a.targetId === entry.targetId);
      if (idx >= 0) arr[idx] = entry;
      else arr.push(entry);
      arr.sort((a, b) => a.requestedAtMs - b.requestedAtMs);
      next.set(entry.sessionId, arr);
      return { bySession: next };
    });
  },

  resolve(sessionId, targetId) {
    set((state) => {
      const arr = state.bySession.get(sessionId);
      if (!arr) return state;
      const filtered = arr.filter((a) => a.targetId !== targetId);
      if (filtered.length === arr.length) return state;
      const next = new Map(state.bySession);
      next.set(sessionId, filtered);
      return { bySession: next };
    });
  },

  clear(sessionId) {
    set((state) => {
      if (!state.bySession.has(sessionId)) return state;
      const next = new Map(state.bySession);
      next.delete(sessionId);
      return { bySession: next };
    });
  },
}));
