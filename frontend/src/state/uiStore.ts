import { create } from 'zustand';

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
}

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
    set((s) => ({ zoomSeconds: Math.max(ZOOM_MIN, Math.round(s.zoomSeconds / 1.5)) })),
  zoomOut: () =>
    set((s) => ({ zoomSeconds: Math.min(ZOOM_MAX, Math.round(s.zoomSeconds * 1.5)) })),
  setZoom: (sec) => set({ zoomSeconds: Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, sec)) }),
  jumpToLive: () => set({ liveFollow: true }),
  togglePause: () => set((s) => ({ paused: !s.paused })),
}));
