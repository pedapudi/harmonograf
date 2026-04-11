import { useEffect, useMemo, useRef, useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import { formatDuration, formatRelativeTime } from '../../lib/format';
import { bucketSessions, mockSessions, type MockSession } from './mockSessions';

const ARCHIVE_PAGE = 50;

export function SessionPicker() {
  const open = useUiStore((s) => s.sessionPickerOpen);
  const close = useUiStore((s) => s.closeSessionPicker);
  const setSession = useUiStore((s) => s.setCurrentSession);
  const [query, setQuery] = useState('');
  const [showArchive, setShowArchive] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery('');
      setShowArchive(false);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const buckets = useMemo(() => {
    const filtered = mockSessions.filter(
      (s) =>
        query.trim() === '' ||
        s.title.toLowerCase().includes(query.toLowerCase()) ||
        s.id.toLowerCase().includes(query.toLowerCase()),
    );
    return bucketSessions(filtered);
  }, [query]);

  if (!open) return null;

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
