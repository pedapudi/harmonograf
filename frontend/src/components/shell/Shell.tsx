import { AppBar } from './AppBar';
import { NavRail } from './NavRail';
import { Drawer } from './Drawer';
import { GanttPlaceholder } from '../Gantt/GanttPlaceholder';
import { Minimap } from '../Minimap/Minimap';
import { TransportBar } from '../TransportBar/TransportBar';
import { SessionPicker } from '../SessionPicker/SessionPicker';
import { HelpOverlay } from './HelpOverlay';
import { useGlobalShortcuts } from '../../lib/shortcuts';

export function Shell() {
  useGlobalShortcuts();
  return (
    <div className="hg-shell">
      <AppBar />
      <NavRail />
      <main className="hg-main">
        <GanttPlaceholder />
        <Minimap />
        <TransportBar />
      </main>
      <Drawer />
      <SessionPicker />
      <HelpOverlay />
    </div>
  );
}
