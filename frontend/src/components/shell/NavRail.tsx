import { useUiStore, type NavSection } from '../../state/uiStore';

interface NavItem {
  id: NavSection;
  label: string;
  icon: string;
}

const ITEMS: NavItem[] = [
  { id: 'sessions', label: 'Sessions', icon: '◫' },
  { id: 'activity', label: 'Activity', icon: '!' },
  { id: 'annotations', label: 'Notes', icon: '✎' },
  { id: 'settings', label: 'Settings', icon: '⚙' },
];

export function NavRail() {
  const open = useUiStore((s) => s.navRailOpen);
  const current = useUiStore((s) => s.navSection);
  const setSection = useUiStore((s) => s.setNavSection);

  return (
    <nav className={`hg-rail${open ? '' : ' hg-rail--collapsed'}`} aria-label="Sections">
      {ITEMS.map((item) => (
        <button
          key={item.id}
          className="hg-rail__item"
          aria-selected={item.id === current}
          onClick={() => setSection(item.id)}
        >
          <span className="hg-rail__icon">{item.icon}</span>
          <span>{item.label}</span>
        </button>
      ))}
    </nav>
  );
}
