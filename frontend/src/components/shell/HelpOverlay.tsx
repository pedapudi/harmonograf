import { useUiStore } from '../../state/uiStore';

interface ShortcutRow {
  keys: string[];
  description: string;
}

const ROWS: ShortcutRow[] = [
  { keys: ['?'], description: 'Toggle this help overlay' },
  { keys: ['⌘K', '/'], description: 'Open session picker' },
  { keys: ['Esc'], description: 'Close overlay / clear selection' },
  { keys: ['Space'], description: 'Toggle pause (all agents)' },
  { keys: ['←', '→'], description: 'Pan 10%' },
  { keys: ['+', '-'], description: 'Zoom in / out' },
  { keys: ['F'], description: 'Fit session to viewport' },
  { keys: ['L'], description: 'Return to live cursor' },
  { keys: ['J', 'K'], description: 'Next / previous span' },
  { keys: ['[', ']'], description: 'Previous / next agent row' },
  { keys: ['G'], description: 'Jump to first agent' },
  { keys: ['⇧G'], description: 'Jump to last agent' },
  { keys: ['A'], description: 'Annotate selected span' },
  { keys: ['S'], description: 'Steer selected span' },
];

export function HelpOverlay() {
  const open = useUiStore((s) => s.helpOpen);
  const close = useUiStore((s) => s.closeHelp);
  if (!open) return null;
  return (
    <div
      className="hg-help-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
      onClick={close}
    >
      <div className="hg-help" onClick={(e) => e.stopPropagation()}>
        <h2>Keyboard shortcuts</h2>
        <dl>
          {ROWS.map((r) => (
            <div
              key={r.description}
              style={{ display: 'contents' }}
            >
              <dt>
                {r.keys.map((k, i) => (
                  <span key={k}>
                    {i > 0 && <span style={{ opacity: 0.5, margin: '0 4px' }}>/</span>}
                    <kbd>{k}</kbd>
                  </span>
                ))}
              </dt>
              <dd>{r.description}</dd>
            </div>
          ))}
        </dl>
        <div className="hg-help__hint">Press Esc or ? to close</div>
      </div>
    </div>
  );
}
