import { useEffect, useMemo, useRef, useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import { formatDuration, formatRelativeTime } from '../../lib/format';
import { bucketSessions, mockSessions, type MockSession } from './mockSessions';
import { useSessions } from '../../rpc/hooks';
import { SessionStatus as PbSessionStatus } from '../../pb/harmonograf/v1/types_pb.js';

const ARCHIVE_PAGE = 50;

function pbToMock(
  s: ReturnType<typeof useSessions>['sessions'][number],
): MockSession {
  const status =
    s.status === PbSessionStatus.LIVE
      ? 'LIVE'
      : s.status === PbSessionStatus.ABORTED
        ? 'ABORTED'
        : 'COMPLETED';
  const last = s.lastActivity
    ? new Date(Number(s.lastActivity.seconds) * 1000)
    : new Date();
  const created = s.createdAt ? Number(s.createdAt.seconds) * 1000 : 0;
  const ended = s.endedAt ? Number(s.endedAt.seconds) * 1000 : last.getTime();
  return {
    id: s.id,
    title: s.title || s.id,
    status,
    agentCount: s.agentCount,
    durationSeconds: Math.max(0, Math.floor((ended - created) / 1000)),
    lastActivity: last,
    attention: s.attentionCount,
  };
}

export function SessionPicker() {
  const open = useUiStore((s) => s.sessionPickerOpen);
  const close = useUiStore((s) => s.closeSessionPicker);
  const setSession = useUiStore((s) => s.setCurrentSession);
  const inputRef = useRef<HTMLInputElement>(null);

  // Real sessions from the server; falls back to mock data when the server
  // isn't reachable (e.g., during local frontend-only development).
  const { sessions: rpcSessions, error: rpcError } = useSessions();

  // Scope picker state to the current open-cycle: remounting via `key` resets
  // `query` and `showArchive` without a setState-in-effect anti-pattern.
  if (!open) return null;
  return <SessionPickerBody
    close={close}
    setSession={setSession}
    inputRef={inputRef}
    rpcSessions={rpcSessions}
    rpcError={rpcError}
  />;
}

function SessionPickerBody({
  close,
  setSession,
  inputRef,
  rpcSessions,
  rpcError,
}: {
  close: () => void;
  setSession: (id: string | null) => void;
  inputRef: React.RefObject<HTMLInputElement | null>;
  rpcSessions: ReturnType<typeof useSessions>['sessions'];
  rpcError: string | null;
}) {
  const [query, setQuery] = useState('');
  const [showArchive, setShowArchive] = useState(false);

  useEffect(() => {
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [inputRef]);

  const effectiveSessions: MockSession[] = useMemo(() => {
    if (rpcError || rpcSessions.length === 0) return mockSessions;
    return rpcSessions.map(pbToMock);
  }, [rpcSessions, rpcError]);

  const buckets = useMemo(() => {
    const filtered = effectiveSessions.filter(
      (s) =>
        query.trim() === '' ||
        s.title.toLowerCase().includes(query.toLowerCase()) ||
        s.id.toLowerCase().includes(query.toLowerCase()),
    );
    return bucketSessions(filtered);
  }, [query, effectiveSessions]);

  const choose = (s: MockSession) => {
    setSession(s.id);
    close();
  };

  return (
    <div
      className="hg-picker__backdrop"
      onClick={close}
      role="dialog"
      aria-label="Session picker"
    >
      <div className="hg-picker" onClick={(e) => e.stopPropagation()}>
        <div className="hg-picker__search">
          <input
            ref={inputRef}
            placeholder="Search sessions…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') close();
            }}
          />
        </div>
        {rpcError && (
          <div
            className="hg-picker__row-sub"
            style={{ padding: '4px 16px', opacity: 0.7 }}
          >
            Server unreachable — showing demo sessions.
          </div>
        )}
        <div className="hg-picker__list">
          {buckets.live.length > 0 && (
            <Section label="Live">
              {buckets.live.map((s) => (
                <SessionRow key={s.id} session={s} onPick={choose} live />
              ))}
            </Section>
          )}
          {buckets.recent.length > 0 && (
            <Section label="Recent (last 24h)">
              {buckets.recent.map((s) => (
                <SessionRow key={s.id} session={s} onPick={choose} />
              ))}
            </Section>
          )}
          {buckets.archive.length > 0 && (
            <Section label="Archive">
              {(showArchive ? buckets.archive : buckets.archive.slice(0, 3)).map((s) => (
                <SessionRow key={s.id} session={s} onPick={choose} />
              ))}
              {buckets.archive.length > 3 && !showArchive && (
                <div
                  className="hg-picker__more"
                  onClick={() => setShowArchive(true)}
                  role="button"
                >
                  Show all ({Math.min(buckets.archive.length, ARCHIVE_PAGE)})
                </div>
              )}
            </Section>
          )}
        </div>
      </div>
    </div>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <div className="hg-picker__section-label">{label}</div>
      {children}
    </>
  );
}

function SessionRow({
  session,
  onPick,
  live = false,
}: {
  session: MockSession;
  onPick: (s: MockSession) => void;
  live?: boolean;
}) {
  return (
    <div className="hg-picker__row" onClick={() => onPick(session)} role="button">
      {live && <span className="hg-transport__live-dot" />}
      <div className="hg-picker__row-meta">
        <div className="hg-picker__row-title">{session.title}</div>
        <div className="hg-picker__row-sub">
          {session.agentCount} agents · {formatDuration(session.durationSeconds)} ·{' '}
          {formatRelativeTime(session.lastActivity)}
        </div>
      </div>
      {session.attention > 0 && (
        <span className="hg-picker__row-attention">{session.attention} need attention</span>
      )}
    </div>
  );
}
