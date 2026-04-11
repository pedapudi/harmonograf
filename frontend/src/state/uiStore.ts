import { create } from 'zustand';
import type { GanttRenderer } from '../gantt/renderer';

export type NavSection = 'sessions' | 'activity' | 'annotations' | 'settings';

interface UiState {
  currentSessionId: string | null;
  selectedSpanId: string | null;
  drawerOpen: boolean;
  navSection: NavSection;
  navRailOpen: boolean;
  sessionPickerOpen: boolean;
  helpOpen: boolean;
  focusedAgentId: string | null;
  zoomSeconds: number; // visible time window in seconds
  liveFollow: boolean;
  paused: boolean;
  // Active Gantt renderer instance, if any. Set by GanttCanvas on mount so
  // chrome (transport bar +/- buttons) can drive the viewport directly. Not
  // reactive — components that need to redraw on viewport changes should
  // subscribe via zoomSeconds, which the renderer pushes back through its
  // onViewportChange callback.
  activeRenderer: GanttRenderer | null;

  setCurrentSession: (id: string | null) => void;
  selectSpan: (id: string | null) => void;
  closeDrawer: () => void;
  setNavSection: (s: NavSection) => void;
  toggleNavRail: () => void;
  openSessionPicker: () => void;
  closeSessionPicker: () => void;
  toggleHelp: () => void;
  closeHelp: () => void;
  setFocusedAgent: (id: string | null) => void;
  zoomIn: () => void;
  zoomOut: () => void;
  setZoom: (sec: number) => void;
  jumpToLive: () => void;
  togglePause: () => void;
  setActiveRenderer: (r: GanttRenderer | null) => void;
}

const ZOOM_STEP = 1.5;

const ZOOM_MIN = 30;
const ZOOM_MAX = 6 * 60 * 60;

export const useUiStore = create<UiState>((set) => ({
  currentSessionId: null,
  selectedSpanId: null,
  drawerOpen: false,
  navSection: 'sessions',
  navRailOpen: true,
  sessionPickerOpen: false,
  helpOpen: false,
  focusedAgentId: null,
  zoomSeconds: 300,
  liveFollow: true,
  paused: false,
  activeRenderer: null,

  setCurrentSession: (id) => set({ currentSessionId: id }),
  selectSpan: (id) => set({ selectedSpanId: id, drawerOpen: id !== null }),
  closeDrawer: () => set({ drawerOpen: false, selectedSpanId: null }),
  setNavSection: (navSection) => set({ navSection }),
  toggleNavRail: () => set((s) => ({ navRailOpen: !s.navRailOpen })),
  openSessionPicker: () => set({ sessionPickerOpen: true }),
  closeSessionPicker: () => set({ sessionPickerOpen: false }),
  toggleHelp: () => set((s) => ({ helpOpen: !s.helpOpen })),
  closeHelp: () => set({ helpOpen: false }),
  setFocusedAgent: (id) => set({ focusedAgentId: id }),
  zoomIn: () =>
    set((s) => {
      s.activeRenderer?.zoomBy(ZOOM_STEP);
      return { zoomSeconds: Math.max(ZOOM_MIN, Math.round(s.zoomSeconds / ZOOM_STEP)) };
    }),
  zoomOut: () =>
    set((s) => {
      s.activeRenderer?.zoomBy(1 / ZOOM_STEP);
      return { zoomSeconds: Math.min(ZOOM_MAX, Math.round(s.zoomSeconds * ZOOM_STEP)) };
    }),
  setZoom: (sec) => set({ zoomSeconds: Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, sec)) }),
  jumpToLive: () => set({ liveFollow: true }),
  togglePause: () => set((s) => ({ paused: !s.paused })),
  setActiveRenderer: (r) => set({ activeRenderer: r }),
}));
