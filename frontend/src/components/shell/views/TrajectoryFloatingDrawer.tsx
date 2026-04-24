// TrajectoryFloatingDrawer — overlay drawer pinned to the right edge of
// the Trajectory view. Lets the DAG stay full-width by default while
// detail panes slide in on demand rather than permanently consuming a
// right column.
//
// Absolute-positioned within the nearest relatively-positioned ancestor
// (the trajectory view's root). The backdrop covers only that ancestor,
// not the full viewport — so opening a drawer does not dim unrelated
// chrome.
//
// Enter/exit is a 200ms translateX slide. When `open=false` the drawer
// unmounts entirely (cheaper than keeping it around + translated off
// screen, and side-steps focus-trap bookkeeping when idle).

import type React from 'react';
import { useCallback, useEffect, useId, useRef } from 'react';

export interface TrajectoryFloatingDrawerProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
  /** Width override in px. Default 400. */
  width?: number;
  /** Optional stable id for the drawer root, useful in tests. */
  testId?: string;
  /** Optional stable id for the close button. Defaults to `${testId}-close`. */
  closeTestId?: string;
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function collectFocusable(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => !el.hasAttribute('data-focus-trap-exit'),
  );
}

export function TrajectoryFloatingDrawer(
  props: TrajectoryFloatingDrawerProps,
): React.ReactElement | null {
  const { open, onClose, title, children, width = 400, testId, closeTestId } = props;
  const drawerRef = useRef<HTMLDivElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const autoTitleId = useId();
  const titleId = title ? `${autoTitleId}-title` : undefined;

  // Track the element focused before the drawer opened so we can restore
  // focus on close (WCAG 2.4.3 — focus order / reasonable return point).
  useEffect(() => {
    if (!open) return;
    previousFocusRef.current =
      (document.activeElement as HTMLElement | null) ?? null;
    // Move focus into the drawer on open so keyboard users land inside.
    const drawer = drawerRef.current;
    if (drawer) {
      const focusables = collectFocusable(drawer);
      const first = focusables[0] ?? drawer;
      first.focus();
    }
    return () => {
      const prev = previousFocusRef.current;
      if (prev && typeof prev.focus === 'function') {
        prev.focus();
      }
      previousFocusRef.current = null;
    };
  }, [open]);

  // Esc closes. Tab / Shift-Tab wrap within the drawer.
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
      } else {
        if (active === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [open, onClose]);

  const onBackdropClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose],
  );

  if (!open) return null;

  return (
    <>
      <div
        className="hg-traj__drawer-backdrop"
        data-testid={testId ? `${testId}-backdrop` : 'trajectory-drawer-backdrop'}
        onClick={onBackdropClick}
        // The backdrop is decorative click-surface, not a dialog trigger.
        aria-hidden="true"
      />
      <div
        ref={drawerRef}
        className="hg-traj__drawer"
        data-testid={testId ?? 'trajectory-drawer'}
        data-open="true"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        style={{ width: `${width}px` }}
      >
        {title && (
          <header className="hg-traj__drawer-head">
            <h3 id={titleId} className="hg-traj__drawer-title">
              {title}
            </h3>
            <button
              type="button"
              className="hg-traj__drawer-close"
              onClick={onClose}
              aria-label="Close"
              data-testid={
                closeTestId ??
                (testId ? `${testId}-close` : 'trajectory-drawer-close')
              }
            >
              ×
            </button>
          </header>
        )}
        <div className="hg-traj__drawer-body">{children}</div>
      </div>
    </>
  );
}
