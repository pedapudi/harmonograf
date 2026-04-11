export type SessionStatus = 'LIVE' | 'COMPLETED' | 'ABORTED';

export interface MockSession {
  id: string;
  title: string;
  status: SessionStatus;
  agentCount: number;
  durationSeconds: number;
  lastActivity: Date;
  attention: number;
}

const now = Date.now();

export const mockSessions: MockSession[] = [
  {
    id: 'sess_2026-04-10_mission-alpha',
    title: 'Mission alpha — research crew',
    status: 'LIVE',
    agentCount: 4,
    durationSeconds: 12 * 60 + 31,
    lastActivity: new Date(now - 2_000),
    attention: 1,
  },
  {
    id: 'sess_2026-04-10_codegen-pipeline',
    title: 'Codegen pipeline shakedown',
    status: 'LIVE',
    agentCount: 7,
    durationSeconds: 47 * 60,
    lastActivity: new Date(now - 15_000),
    attention: 0,
  },
  {
    id: 'sess_2026-04-10_0003',
    title: 'Backfill job — overnight',
    status: 'COMPLETED',
    agentCount: 2,
    durationSeconds: 3 * 60 * 60 + 11 * 60,
    lastActivity: new Date(now - 4 * 60 * 60 * 1000),
    attention: 0,
  },
  {
    id: 'sess_2026-04-09_planning',
    title: 'Planning session — Q2 roadmap',
    status: 'COMPLETED',
    agentCount: 5,
    durationSeconds: 58 * 60,
    lastActivity: new Date(now - 22 * 60 * 60 * 1000),
    attention: 0,
  },
  {
    id: 'sess_2026-04-08_eval-run',
    title: 'Eval run — bench v3',
    status: 'COMPLETED',
    agentCount: 1,
    durationSeconds: 17 * 60,
    lastActivity: new Date(now - 2 * 24 * 60 * 60 * 1000),
    attention: 0,
  },
  {
    id: 'sess_2026-03-30_old',
    title: 'Old debug session',
    status: 'ABORTED',
    agentCount: 3,
    durationSeconds: 4 * 60,
    lastActivity: new Date(now - 11 * 24 * 60 * 60 * 1000),
    attention: 0,
  },
];

export function bucketSessions(sessions: MockSession[]) {
  const live: MockSession[] = [];
  const recent: MockSession[] = [];
  const archive: MockSession[] = [];
  const cutoff = Date.now() - 24 * 60 * 60 * 1000;
  for (const s of sessions) {
    if (s.status === 'LIVE') live.push(s);
    else if (s.lastActivity.getTime() >= cutoff) recent.push(s);
    else archive.push(s);
  }
  live.sort((a, b) => b.lastActivity.getTime() - a.lastActivity.getTime());
  return { live, recent, archive };
}
