import { AppBar } from './AppBar';
import { CurrentTaskStrip } from './CurrentTaskStrip';
import { PlanRevisionBanner } from './PlanRevisionBanner';
import { NavRail } from './NavRail';
import { Drawer } from './Drawer';
import { SessionPicker } from '../SessionPicker/SessionPicker';
import { HelpOverlay } from './HelpOverlay';
import { GanttLegend } from '../Gantt/GanttLegend';
import { ShellErrorBoundary } from './ErrorBoundary';
import { ApprovalDrawer } from '../Interaction/ApprovalDrawer';
import { SessionsSyncer } from '../../rpc/SessionsSyncer';
import { useGlobalShortcuts } from '../../lib/shortcuts';
import { useUiStore } from '../../state/uiStore';
import { ActivityView } from './views/ActivityView';
import { GanttView } from './views/GanttView';
import { GraphView } from './views/GraphView';
import { NotesView } from './views/NotesView';
import { SettingsView } from './views/SettingsView';

export function Shell() {
  useGlobalShortcuts();
  const navSection = useUiStore((s) => s.navSection);
  return (
    <div className="hg-shell">
      <SessionsSyncer />
      <AppBar />
      <CurrentTaskStrip />
      <PlanRevisionBanner />
      <NavRail />
      <main className="hg-main" data-testid="main-content" data-view={navSection}>
        <ShellErrorBoundary key={navSection}>
          {navSection === 'sessions' && <GanttView />}
          {navSection === 'activity' && <ActivityView />}
          {navSection === 'graph' && <GraphView />}
          {navSection === 'annotations' && <NotesView />}
          {navSection === 'settings' && <SettingsView />}
        </ShellErrorBoundary>
      </main>
      <Drawer />
      <SessionPicker />
      <HelpOverlay />
      <GanttLegend />
      <ApprovalDrawer />
    </div>
  );
}
