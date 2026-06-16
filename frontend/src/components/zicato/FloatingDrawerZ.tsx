// FloatingDrawerZ.tsx — the zicato floating detail drawer. A port of the MD3
// TrajectoryFloatingDrawer (focus-trap + Esc + backdrop + 200ms slide) restyled
// in the zicato language (Tufte line-art, token-only colours, monospace). It
// overlays the main area rather than reserving a right column, and surfaces the
// steering detail:
//
//   * STEERING — a goldfive correction: what triggered it (drift kind +
//                severity), what it decided (reason), and the agent/task it
//                steered (the ZSteer the user clicked on the Gantt arrow).
//
// (Reasoning is rendered inline in the docked inspector — see ZicatoConsole —
// so there is no separate floating reasoning body here.)
//
// It is mounted INSIDE the .zk-root subtree (ZicatoConsole) so it inherits the
// scoped theme and never touches the MD3 console. When `open=false` the drawer
// unmounts entirely.

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  type MouseEvent as ReactMouseEvent,
  type ReactElement,
  type ReactNode,
} from 'react';
import type { ZSession, ZSteer } from './adapter';
import { steerColor } from './svgUtils';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function collectFocusable(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
}

export interface FloatingDrawerZProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: ReactNode;
  /** Width override in px. Default 360 (matches the zicato chrome scale). */
  width?: number;
  testId?: string;
}

/**
 * The bare floating drawer chrome: backdrop + sliding panel + focus trap. The
 * body content (reasoning / steering) is passed as children so callers compose
 * exactly what they need.
 */
export function FloatingDrawerZ(props: FloatingDrawerZProps): ReactElement | null {
  const { open, onClose, title, children, width = 360, testId } = props;
  const drawerRef = useRef<HTMLDivElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const autoTitleId = useId();
  const titleId = title ? `${autoTitleId}-title` : undefined;

  // Restore focus on close (WCAG 2.4.3) and move focus into the drawer on open.
  useEffect(() => {
    if (!open) return;
    previousFocusRef.current =
      (document.activeElement as HTMLElement | null) ?? null;
    const drawer = drawerRef.current;
    if (drawer) {
      const focusables = collectFocusable(drawer);
      (focusables[0] ?? drawer).focus();
    }
    return () => {
      const prev = previousFocusRef.current;
      if (prev && typeof prev.focus === 'function') prev.focus();
      previousFocusRef.current = null;
    };
  }, [open]);

  // Esc closes; Tab / Shift-Tab wrap within the drawer.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== 'Tab') return;
      const drawer = drawerRef.current;
      if (!drawer) return;
      const focusables = collectFocusable(drawer);
      if (focusables.length === 0) {
        e.preventDefault();
        drawer.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (active === first || !drawer.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [open, onClose]);

  const onBackdropClick = useCallback(
    (e: ReactMouseEvent<HTMLDivElement>) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose],
  );

  if (!open) return null;

  return (
    <>
      <div
        className="zk-fdrawer-backdrop"
        data-testid={testId ? `${testId}-backdrop` : 'zk-fdrawer-backdrop'}
        onClick={onBackdropClick}
        aria-hidden="true"
      />
      <div
        ref={drawerRef}
        className="zk-fdrawer"
        data-testid={testId ?? 'zk-fdrawer'}
        data-open="true"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        style={{ width: `${width}px` }}
      >
        <header className="zk-fdrawer-head">
          <h3 id={titleId} className="zk-fdrawer-title">
            {title ?? 'detail'}
          </h3>
          <button
            type="button"
            className="zk-fdrawer-close"
            onClick={onClose}
            aria-label="Close"
            data-testid={testId ? `${testId}-close` : 'zk-fdrawer-close'}
          >
            ×
          </button>
        </header>
        <div className="zk-fdrawer-body">{children}</div>
      </div>
    </>
  );
}

// ── detail bodies ────────────────────────────────────────────────────────────

/** Resolve a lane label from an agent id (falls back to the id itself). */
function labelOf(z: ZSession, agentId: string): string {
  return z.agents.find((a) => a.id === agentId)?.label ?? agentId;
}

export interface SteeringDetailBodyZProps {
  steer: ZSteer;
  z: ZSession;
}

/**
 * The steering detail: the three questions the MD3 SteeringDetailPanel answers,
 * in the zicato language —
 *   Trigger  — drift kind + severity that goldfive observed.
 *   Steering — the reason / decision text goldfive published.
 *   Target   — the agent (+ task) the correction steered.
 */
export function SteeringDetailBodyZ(
  props: SteeringDetailBodyZProps,
): ReactElement {
  const { steer, z } = props;
  const sevTone = steerColor(steer);
  return (
    <div data-testid="zk-steering-detail">
      <div className="zk-detail-kicker" style={{ color: sevTone }}>
        goldfive steer{steer.revision > 0 ? ` · rev ${steer.revision}` : ''}
      </div>
      <section className="zk-detail-section" data-testid="zk-steering-trigger">
        <h4>trigger</h4>
        <div className="zk-detail-target">
          {steer.kind || 'drift'}
          {steer.severity ? ` (${steer.severity})` : ''}
        </div>
      </section>
      <section className="zk-detail-section" data-testid="zk-steering-steering">
        <h4>steering</h4>
        <div className="zk-detail-target">
          {steer.reason || '(no reason recorded)'}
        </div>
      </section>
      <section className="zk-detail-section" data-testid="zk-steering-target">
        <h4>target</h4>
        <div className="zk-detail-target" data-testid="zk-steering-target-agent">
          <span className="zk-detail-target-label">agent</span>
          {labelOf(z, steer.to)}
        </div>
        {steer.taskId && (
          <div
            className="zk-detail-target"
            data-testid="zk-steering-target-task"
            style={{ marginTop: 4 }}
          >
            <span className="zk-detail-target-label">task</span>
            <code>{steer.taskId}</code>
          </div>
        )}
      </section>
    </div>
  );
}
