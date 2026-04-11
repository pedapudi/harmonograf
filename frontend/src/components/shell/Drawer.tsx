import { useUiStore } from '../../state/uiStore';

export function Drawer() {
  const open = useUiStore((s) => s.drawerOpen);
  const selected = useUiStore((s) => s.selectedSpanId);
  const close = useUiStore((s) => s.closeDrawer);

  return (
    <aside className={`hg-drawer${open ? ' hg-drawer--open' : ''}`} aria-hidden={!open}>
      <div className="hg-drawer__inner">
        <div className="hg-drawer__header">
          <div className="hg-drawer__title">{selected ?? 'Inspector'}</div>
          <button className="hg-appbar__icon-btn" onClick={close} aria-label="Close drawer">
            ✕
          </button>
        </div>
        <div className="hg-drawer__body">
          {selected ? (
            <p>
              Inspector pane for <code>{selected}</code>. Tabs (Overview, Payload, Approval,
              Annotations, Raw) land in task #13.
            </p>
          ) : (
            <p>Select a span on the Gantt to inspect it.</p>
          )}
        </div>
      </div>
    </aside>
  );
}
