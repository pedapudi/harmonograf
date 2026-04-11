import { AppBar } from './AppBar';
import { NavRail } from './NavRail';
import { Drawer } from './Drawer';
import { GanttPlaceholder } from '../Gantt/GanttPlaceholder';
import { TransportBar } from '../TransportBar/TransportBar';
import { SessionPicker } from '../SessionPicker/SessionPicker';
import { HelpOverlay } from './HelpOverlay';
import { SessionsSyncer } from '../../rpc/SessionsSyncer';
import { useGlobalShortcuts } from '../../lib/shortcuts';
import { useUiStore } from '../../state/uiStore';
import { ActivityView } from './views/ActivityView';
import { NotesView } from './views/NotesView';
import { SettingsView } from './views/SettingsView';

export function Shell() {
  useGlobalShortcuts();
  const navSection = useUiStore((s) => s.navSection);
  return (
    <div className="hg-shell">
      <SessionsSyncer />
      <AppBar />
      <NavRail />
      <main className="hg-main" data-testid="main-content" data-view={navSection}>
        {navSection === 'sessions' && (
          <>
            <GanttPlaceholder />
            <TransportBar />
          </>
        )}
        {navSection === 'activity' && <ActivityView />}
        {navSection === 'annotations' && <NotesView />}
        {navSection === 'settings' && <SettingsView />}
      </main>
      <Drawer />
      <SessionPicker />
      <HelpOverlay />
    </div>
  );
}
