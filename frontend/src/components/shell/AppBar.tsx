import { useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import { mockSessions } from '../SessionPicker/mockSessions';
import { useSessionsStore } from '../../state/sessionsStore';
import { useThemeStore } from '../../theme/store';
import type { ColorBlindMode, ThemeBase } from '../../theme/themes';

const THEMES: { id: ThemeBase; label: string }[] = [
  { id: 'dark', label: 'Dark' },
  { id: 'light', label: 'Light' },
  { id: 'amoled', label: 'AMOLED' },
  { id: 'high-contrast', label: 'High contrast' },
];

const COLOR_BLIND: { id: ColorBlindMode; label: string }[] = [
  { id: 'none', label: 'None' },
  { id: 'deuteranopia', label: 'Deuteranopia' },
  { id: 'protanopia', label: 'Protanopia' },
  { id: 'tritanopia', label: 'Tritanopia' },
];

export function AppBar() {
  const openPicker = useUiStore((s) => s.openSessionPicker);
  const toggleRail = useUiStore((s) => s.toggleNavRail);
  const sessionId = useUiStore((s) => s.currentSessionId);
  const rpcSessions = useSessionsStore((s) => s.sessions);
  const rpcError = useSessionsStore((s) => s.error);
  const [themeMenuOpen, setThemeMenuOpen] = useState(false);
  const themeBase = useThemeStore((s) => s.base);
  const colorBlind = useThemeStore((s) => s.colorBlind);
  const setBase = useThemeStore((s) => s.setBase);
  const setColorBlind = useThemeStore((s) => s.setColorBlind);

  // Real sessions when the server is reachable; mock sessions only as a
  // dev-mode fallback (same policy as SessionPicker).
  const rpcTitle = rpcSessions.find((s) => s.id === sessionId)?.title;
  const mockTitle = mockSessions.find((s) => s.id === sessionId)?.title;
  const sessionTitle =
    (rpcTitle && rpcTitle.length > 0 ? rpcTitle : undefined) ??
    (rpcError ? mockTitle : undefined) ??
    'Select session';
  const attention = rpcError
    ? mockSessions.reduce((acc, s) => acc + s.attention, 0)
    : rpcSessions.reduce((acc, s) => acc + s.attentionCount, 0);

  return (
    <header className="hg-appbar" data-testid="app-bar">
      <button
        className="hg-appbar__icon-btn"
        onClick={toggleRail}
        aria-label="Toggle navigation"
      >
        ☰
      </button>
      <div className="hg-appbar__title">Harmonograf</div>
      <button
        className="hg-appbar__session-trigger"
        onClick={openPicker}
        data-testid="session-picker"
      >
        <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {sessionTitle}
        </span>
        <span aria-hidden>▾</span>
      </button>
      <span className="hg-picker__row-sub" style={{ marginLeft: 8 }}>
        ⌘K
      </span>
      <div className="hg-appbar__spacer" />
      <button
        className="hg-appbar__icon-btn hg-appbar__attention-badge"
        data-count={attention}
        aria-label={`Activity queue (${attention})`}
        title={`${attention} need attention`}
      >
        🔔
      </button>
      <button
        className="hg-appbar__icon-btn"
        onClick={() => setThemeMenuOpen((v) => !v)}
        aria-label="Theme"
        title="Theme"
      >
        ◐
      </button>
      <button className="hg-appbar__icon-btn" aria-label="User menu">
        ⚙
      </button>
      {themeMenuOpen && (
        <div className="hg-theme-menu" onMouseLeave={() => setThemeMenuOpen(false)}>
          <div className="hg-theme-menu__group">Theme</div>
          {THEMES.map((t) => (
            <button
              key={t.id}
              className="hg-theme-menu__item"
              aria-selected={t.id === themeBase}
              onClick={() => setBase(t.id)}
            >
              {t.label}
            </button>
          ))}
          <div className="hg-theme-menu__group">Color vision</div>
          {COLOR_BLIND.map((m) => (
            <button
              key={m.id}
              className="hg-theme-menu__item"
              aria-selected={m.id === colorBlind}
              onClick={() => setColorBlind(m.id)}
            >
              {m.label}
            </button>
          ))}
        </div>
      )}
    </header>
  );
}
