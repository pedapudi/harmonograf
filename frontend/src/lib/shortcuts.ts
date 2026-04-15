import { useEffect } from 'react';
import { useUiStore } from '../state/uiStore';
import { getSessionStore } from '../rpc/hooks';
import type { Span } from '../gantt/types';

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

// Gather all spans across all agents in the current session, ordered by
// startMs then by id for a stable traversal under j/k navigation.
function listAllSpans(sessionId: string): Span[] {
  const store = getSessionStore(sessionId);
  if (!store) return [];
  const out: Span[] = [];
  for (const agent of store.agents.list) {
    const spans = store.spans.queryAgent(
      agent.id,
      -Number.MAX_SAFE_INTEGER,
      Number.MAX_SAFE_INTEGER,
    );
    out.push(...spans);
  }
  out.sort((a, b) => (a.startMs - b.startMs) || a.id.localeCompare(b.id));
  return out;
}

function neighborSpan(sessionId: string, currentId: string | null, delta: 1 | -1): string | null {
  const spans = listAllSpans(sessionId);
  if (spans.length === 0) return null;
  if (!currentId) return spans[delta > 0 ? 0 : spans.length - 1]?.id ?? null;
  const idx = spans.findIndex((s) => s.id === currentId);
  if (idx < 0) return spans[0].id;
  const next = idx + delta;
  if (next < 0 || next >= spans.length) return null;
  return spans[next].id;
}

function neighborAgent(sessionId: string, currentId: string | null, delta: 1 | -1): string | null {
  const store = getSessionStore(sessionId);
  if (!store) return null;
  const agents = store.agents.list;
  if (agents.length === 0) return null;
  if (!currentId) return agents[delta > 0 ? 0 : agents.length - 1].id;
  const idx = agents.findIndex((a) => a.id === currentId);
  if (idx < 0) return agents[0].id;
  const next = Math.max(0, Math.min(agents.length - 1, idx + delta));
  return agents[next].id;
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
      handler: () => ui().toggleLiveFollow(),
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
      id: 'graph-zoom-in',
      description: 'Zoom in (graph view)',
      combo: 'mod+=',
      handler: () => {
        const s = ui();
        if (s.navSection === 'graph') s.graphActions?.zoomIn();
      },
    },
    {
      id: 'graph-zoom-in-plus',
      description: 'Zoom in (graph view)',
      combo: 'mod++',
      handler: () => {
        const s = ui();
        if (s.navSection === 'graph') s.graphActions?.zoomIn();
      },
    },
    {
      id: 'graph-zoom-out',
      description: 'Zoom out (graph view)',
      combo: 'mod+-',
      handler: () => {
        const s = ui();
        if (s.navSection === 'graph') s.graphActions?.zoomOut();
      },
    },
    {
      id: 'graph-zoom-reset',
      description: 'Reset zoom (graph view)',
      combo: 'mod+0',
      handler: () => {
        const s = ui();
        if (s.navSection === 'graph') s.graphActions?.zoomReset();
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
      id: 'next-span',
      description: 'Select next span',
      combo: 'j',
      handler: () => {
        const s = ui();
        if (!s.currentSessionId) return;
        const next = neighborSpan(s.currentSessionId, s.selectedSpanId, 1);
        if (next) s.selectSpan(next);
      },
    },
    {
      id: 'prev-span',
      description: 'Select previous span',
      combo: 'k',
      handler: () => {
        const s = ui();
        if (!s.currentSessionId) return;
        const next = neighborSpan(s.currentSessionId, s.selectedSpanId, -1);
        if (next) s.selectSpan(next);
      },
    },
    {
      id: 'prev-agent',
      description: 'Focus previous agent row',
      combo: '[',
      handler: () => {
        const s = ui();
        if (!s.currentSessionId) return;
        const next = neighborAgent(s.currentSessionId, s.focusedAgentId, -1);
        s.setFocusedAgent(next);
      },
    },
    {
      id: 'next-agent',
      description: 'Focus next agent row',
      combo: ']',
      handler: () => {
        const s = ui();
        if (!s.currentSessionId) return;
        const next = neighborAgent(s.currentSessionId, s.focusedAgentId, 1);
        s.setFocusedAgent(next);
      },
    },
    {
      id: 'first-agent',
      description: 'Jump to first agent row',
      combo: 'g',
      handler: () => {
        const s = ui();
        if (!s.currentSessionId) return;
        const store = getSessionStore(s.currentSessionId);
        const first = store?.agents.list[0];
        if (first) s.setFocusedAgent(first.id);
      },
    },
    {
      id: 'last-agent',
      description: 'Jump to last agent row',
      combo: 'shift+g',
      handler: () => {
        const s = ui();
        if (!s.currentSessionId) return;
        const store = getSessionStore(s.currentSessionId);
        const agents = store?.agents.list ?? [];
        const last = agents[agents.length - 1];
        if (last) s.setFocusedAgent(last.id);
      },
    },
    {
      id: 'search',
      description: 'Search (open session picker)',
      combo: '/',
      handler: () => useUiStore.setState({ sessionPickerOpen: true }),
    },
    {
      id: 'help',
      description: 'Toggle keyboard help',
      combo: 'shift+?',
      handler: () => ui().toggleHelp(),
    },
    {
      id: 'thinking-trajectory',
      description: 'Open drawer on current span\u2019s Trajectory tab',
      combo: 't',
      handler: () => {
        const s = ui();
        if (!s.currentSessionId) return;
        // Prefer the currently selected span; fall back to the first span
        // in the session so the shortcut always opens something useful
        // (otherwise first-time users mashing "t" would see a no-op).
        const spanId =
          s.selectedSpanId ??
          listAllSpans(s.currentSessionId)[0]?.id ??
          null;
        if (!spanId) return;
        s.openDrawerOnTrajectory(spanId);
      },
    },
    {
      id: 'escape',
      description: 'Close overlay / clear selection',
      combo: 'escape',
      handler: () => {
        const s = ui();
        if (s.helpOpen) s.closeHelp();
        else if (s.sessionPickerOpen) s.closeSessionPicker();
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
