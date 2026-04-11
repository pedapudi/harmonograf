import { useEffect } from 'react';
import { useUiStore } from '../state/uiStore';

// Keyboard shortcut table per doc 04 §7.7. Single global handler installed once
// at app mount. Re-mappable in settings later — for now the table is the source
// of truth and the settings page (when built) will read/write the same map.

export interface ShortcutBinding {
  id: string;
  description: string;
  // Keyboard combo as `KeyboardEvent.key` (case-insensitive) optionally prefixed
  // with `mod+` (Cmd on macOS, Ctrl elsewhere) or `shift+`.
  combo: string;
  handler: () => void;
}

function isMac(): boolean {
  return typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);
}

function comboMatches(combo: string, e: KeyboardEvent): boolean {
  const parts = combo.toLowerCase().split('+');
  const key = parts.pop()!;
  const wantMod = parts.includes('mod');
  const wantShift = parts.includes('shift');
  const wantAlt = parts.includes('alt');
  const modPressed = isMac() ? e.metaKey : e.ctrlKey;
  if (wantMod !== modPressed) return false;
  if (wantShift !== e.shiftKey) return false;
  if (wantAlt !== e.altKey) return false;
  return e.key.toLowerCase() === key;
}

export function defaultShortcuts(): ShortcutBinding[] {
  const ui = useUiStore.getState;
  return [
    {
      id: 'session-picker',
      description: 'Open session picker',
      combo: 'mod+k',
      handler: () => useUiStore.setState({ sessionPickerOpen: true }),
    },
    {
      id: 'toggle-pause',
      description: 'Toggle pause (all agents)',
      combo: ' ',
      handler: () => ui().togglePause(),
    },
    {
      id: 'pan-left',
      description: 'Pan 10% left',
      combo: 'arrowleft',
      handler: () => {
        /* pan handled by Gantt in task #11 */
      },
    },
    {
      id: 'pan-right',
      description: 'Pan 10% right',
      combo: 'arrowright',
      handler: () => {
        /* pan handled by Gantt in task #11 */
      },
    },
    {
      id: 'zoom-in',
      description: 'Zoom in',
      combo: '+',
      handler: () => ui().zoomIn(),
    },
    {
      id: 'zoom-in-eq',
      description: 'Zoom in',
      combo: '=',
      handler: () => ui().zoomIn(),
    },
    {
      id: 'zoom-out',
      description: 'Zoom out',
      combo: '-',
      handler: () => ui().zoomOut(),
    },
    {
      id: 'fit',
      description: 'Fit session to viewport',
      combo: 'f',
      handler: () => ui().setZoom(60 * 60),
    },
    {
      id: 'live',
      description: 'Return to live cursor',
      combo: 'l',
      handler: () => ui().jumpToLive(),
    },
    {
      id: 'graph',
      description: 'Graph mode on selected span',
      combo: 'g',
      handler: () => {
        /* implemented in task #14 */
      },
    },
    {
      id: 'annotate',
      description: 'Annotate selected span',
      combo: 'a',
      handler: () => {
        /* implemented in task #14 */
      },
    },
    {
      id: 'steer',
      description: 'Steer selected span',
      combo: 's',
      handler: () => {
        /* implemented in task #14 */
      },
    },
    {
      id: 'escape',
      description: 'Close drawer / clear selection',
      combo: 'escape',
      handler: () => {
        const s = ui();
        if (s.sessionPickerOpen) s.closeSessionPicker();
        else s.closeDrawer();
      },
    },
  ];
}

export function useGlobalShortcuts(): void {
  useEffect(() => {
    const bindings = defaultShortcuts();
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.isContentEditable ||
          target.tagName === 'MD-FILLED-TEXT-FIELD' ||
          target.tagName === 'MD-OUTLINED-TEXT-FIELD')
      ) {
        // Allow Esc and ⌘K through editable fields
        if (e.key !== 'Escape' && !(e.key.toLowerCase() === 'k' && (e.metaKey || e.ctrlKey))) {
          return;
        }
      }
      for (const b of bindings) {
        if (comboMatches(b.combo, e)) {
          e.preventDefault();
          b.handler();
          return;
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
}
