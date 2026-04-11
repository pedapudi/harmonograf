import { useUiStore } from '../../../state/uiStore';
import { useThemeStore } from '../../../theme/store';
import type { ThemeBase } from '../../../theme/themes';
import { apiBaseUrl } from '../../../rpc/transport';

const HARMONOGRAF_VERSION = '0.0.0';
const REPO_URL = 'https://github.com/anthropics/harmonograf';

const THEMES: { id: ThemeBase; label: string }[] = [
  { id: 'dark', label: 'Dark' },
  { id: 'amoled', label: 'AMOLED' },
  { id: 'light', label: 'Light' },
];

const WINDOWS: { value: number; label: string }[] = [
  { value: 60, label: '1m' },
  { value: 300, label: '5m' },
  { value: 900, label: '15m' },
  { value: 3600, label: '1h' },
];

export function SettingsView() {
  const themeBase = useThemeStore((s) => s.base);
  const setBase = useThemeStore((s) => s.setBase);
  const zoomSeconds = useUiStore((s) => s.zoomSeconds);
  const setZoom = useUiStore((s) => s.setZoom);

  return (
    <section className="hg-panel" data-testid="settings-view">
      <header className="hg-panel__header">
        <h2 className="hg-panel__title">Settings</h2>
      </header>
      <div className="hg-panel__body">
        <div className="hg-settings__group" data-testid="settings-theme">
          <div className="hg-settings__label">Theme</div>
          <div className="hg-settings__row">
            {THEMES.map((t) => (
              <button
                key={t.id}
                className="hg-settings__chip"
                aria-selected={t.id === themeBase}
                onClick={() => setBase(t.id)}
                data-testid={`settings-theme-${t.id}`}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        <div className="hg-settings__group" data-testid="settings-window">
          <div className="hg-settings__label">Default window duration</div>
          <select
            className="hg-settings__select"
            value={WINDOWS.some((w) => w.value === zoomSeconds) ? zoomSeconds : 300}
            onChange={(e) => setZoom(Number(e.target.value))}
            data-testid="settings-window-select"
          >
            {WINDOWS.map((w) => (
              <option key={w.value} value={w.value}>
                {w.label}
              </option>
            ))}
          </select>
        </div>

        <div className="hg-settings__group" data-testid="settings-connection">
          <div className="hg-settings__label">Server connection</div>
          <div className="hg-settings__value">Connected to {apiBaseUrl()}</div>
        </div>

        <div className="hg-settings__group" data-testid="settings-about">
          <div className="hg-settings__label">About</div>
          <div className="hg-settings__value">harmonograf v{HARMONOGRAF_VERSION}</div>
          <a className="hg-settings__value" href={REPO_URL} target="_blank" rel="noreferrer">
            {REPO_URL}
          </a>
        </div>
      </div>
    </section>
  );
}
