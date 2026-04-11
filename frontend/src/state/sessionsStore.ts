import { create } from 'zustand';
import type { SessionSummary } from '../pb/harmonograf/v1/frontend_pb.js';

export type RpcSession = SessionSummary;

interface SessionsStoreState {
  sessions: RpcSession[];
  loading: boolean;
  error: string | null;
  setSessions: (sessions: RpcSession[], error: string | null, loading: boolean) => void;
}

export const useSessionsStore = create<SessionsStoreState>((set) => ({
  sessions: [],
  loading: true,
  error: null,
  setSessions: (sessions, error, loading) => set({ sessions, error, loading }),
}));

export function sessionCreatedAtMs(s: RpcSession): number {
  if (!s.createdAt) return 0;
  return (
    Number(s.createdAt.seconds) * 1000 +
    Math.floor(s.createdAt.nanos / 1_000_000)
  );
}
