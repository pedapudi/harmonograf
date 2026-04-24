import { create } from 'zustand';
import type { GanttRenderer } from '../gantt/renderer';
import type { Viewport as GraphViewport } from '../components/shell/views/graphViewport';
import { clampScale } from '../components/shell/views/graphViewport';

export type NavSection =
  | 'sessions'
  | 'activity'
  | 'graph'
  | 'trajectory'
  | 'annotations'
  | 'settings';

export type TaskPlanMode = 'pre-strip' | 'ghost' | 'hybrid';

// Task/Plan panel view mode. 'cumulative' renders the union-DAG across
// revisions (default, with generation badges + supersedes edges).
// 'latest' renders only the latest revision of each plan (legacy view).
export type TaskPlanView = 'cumulative' | 'latest';

// Persisted task-plan UI preferences. Read on store creation, written on
// mutation. A direct localStorage touchpoint (no middleware) keeps this simple
// and keeps the single-file store tree unchanged.
const TASK_PLAN_MODE_KEY = 'harmonograf.taskPlanMode';
const TASK_PLAN_VISIBLE_KEY = 'harmonograf.taskPlanVisible';
const TASK_PLAN_VIEW_KEY = 'harmonograf.taskPlanView';
const GRAPH_VIEWPORT_KEY = 'harmonograf.graphViewport';
const CONTEXT_OVERLAY_VISIBLE_KEY = 'harmonograf.contextOverlayVisible';
const INTERVENTION_BANDS_VISIBLE_KEY = 'harmonograf.interventionBandsVisible';
// harmonograf: TrajectoryView legacy stacked-layout toggle. Default false
// (new compact layout); persisted so a user who expands it doesn't lose the
// state on reload.
const TRAJECTORY_LEGACY_EXPANDED_KEY = 'harmonograf.trajectoryLegacyExpanded';

function readGraphViewport(): GraphViewport | null {
  try {
    const raw = localStorage.getItem(GRAPH_VIEWPORT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<GraphViewport> | null;
    if (!parsed || typeof parsed !== 'object') return null;
    const { scale, tx, ty } = parsed;
    if (
      typeof scale !== 'number' ||
      typeof tx !== 'number' ||
      typeof ty !== 'number' ||
      !Number.isFinite(scale) ||
      !Number.isFinite(tx) ||
      !Number.isFinite(ty)
    ) {
      return null;
    }
    return { scale: clampScale(scale), tx, ty };
  } catch {
    return null;
  }
}

function writeGraphViewport(vp: GraphViewport | null): void {
  try {
    if (vp === null) localStorage.removeItem(GRAPH_VIEWPORT_KEY);
    else localStorage.setItem(GRAPH_VIEWPORT_KEY, JSON.stringify(vp));
  } catch {
    /* ignore */
  }
}

function readTaskPlanMode(): TaskPlanMode {
  try {
    const v = localStorage.getItem(TASK_PLAN_MODE_KEY);
    if (v === 'pre-strip' || v === 'ghost' || v === 'hybrid') return v;
  } catch {
    /* ignore (SSR / privacy mode) */
  }
  return 'pre-strip';
}

function readTaskPlanVisible(): boolean {
  try {
    const v = localStorage.getItem(TASK_PLAN_VISIBLE_KEY);
    if (v === 'true') return true;
    if (v === 'false') return false;
  } catch {
    /* ignore */
  }
  return true;
}

function writeTaskPlanMode(m: TaskPlanMode): void {
  try {
    localStorage.setItem(TASK_PLAN_MODE_KEY, m);
  } catch {
    /* ignore */
  }
}

function writeTaskPlanVisible(v: boolean): void {
  try {
    localStorage.setItem(TASK_PLAN_VISIBLE_KEY, v ? 'true' : 'false');
  } catch {
    /* ignore */
  }
}

function readTaskPlanView(): TaskPlanView {
  try {
    const v = localStorage.getItem(TASK_PLAN_VIEW_KEY);
    if (v === 'cumulative' || v === 'latest') return v;
  } catch {
    /* ignore */
  }
  return 'cumulative';
}

function writeTaskPlanView(v: TaskPlanView): void {
  try {
    localStorage.setItem(TASK_PLAN_VIEW_KEY, v);
  } catch {
    /* ignore */
  }
}

function readContextOverlayVisible(): boolean {
  try {
    const v = localStorage.getItem(CONTEXT_OVERLAY_VISIBLE_KEY);
    if (v === 'true') return true;
    if (v === 'false') return false;
  } catch {
    /* ignore */
  }
  return true;
}

function writeContextOverlayVisible(v: boolean): void {
  try {
    localStorage.setItem(CONTEXT_OVERLAY_VISIBLE_KEY, v ? 'true' : 'false');
  } catch {
    /* ignore */
  }
}

function readInterventionBandsVisible(): boolean {
  try {
    const v = localStorage.getItem(INTERVENTION_BANDS_VISIBLE_KEY);
    if (v === 'true') return true;
    if (v === 'false') return false;
  } catch {
    /* ignore */
  }
  return true;
}

function writeInterventionBandsVisible(v: boolean): void {
  try {
    localStorage.setItem(INTERVENTION_BANDS_VISIBLE_KEY, v ? 'true' : 'false');
  } catch {
    /* ignore */
  }
}

function readTrajectoryLegacyExpanded(): boolean {
  try {
    const v = localStorage.getItem(TRAJECTORY_LEGACY_EXPANDED_KEY);
    if (v === 'true') return true;
    if (v === 'false') return false;
  } catch {
    /* ignore */
  }
  return false;
}

function writeTrajectoryLegacyExpanded(v: boolean): void {
  try {
    localStorage.setItem(TRAJECTORY_LEGACY_EXPANDED_KEY, v ? 'true' : 'false');
  } catch {
    /* ignore */
  }
}

export type DrawerTabId =
  | 'summary'
  | 'task'
  | 'payload'
  | 'timeline'
  | 'links'
  | 'annotations'
  | 'control';

export type DrawerTaskSubtab = 'overview' | 'trajectory';

interface UiState {
  currentSessionId: string | null;
  selectedSpanId: string | null;
  selectedTaskId: string | null;
  drawerOpen: boolean;
  // Deep-link hint: components listening to these pop them once consumed so
  // the next selection doesn't keep reopening the trajectory tab.
  drawerRequestedTab: DrawerTabId | null;
  drawerRequestedTaskSubtab: DrawerTaskSubtab | null;
  navSection: NavSection;
  navRailOpen: boolean;
  sessionPickerOpen: boolean;
  helpOpen: boolean;
  legendOpen: boolean;
  focusedAgentId: string | null;
  hiddenAgentIds: Set<string>;
  zoomSeconds: number; // visible time window in seconds
  liveFollow: boolean;
  // Active Gantt renderer instance, if any. Set by GanttCanvas on mount so
  // chrome (transport bar +/- buttons) can drive the viewport directly. Not
  // reactive — components that need to redraw on viewport changes should
  // subscribe via zoomSeconds, which the renderer pushes back through its
  // onViewportChange callback.
  activeRenderer: GanttRenderer | null;
  liveActivityCollapsed: boolean;
  toggleLiveActivity: () => void;

  setCurrentSession: (id: string | null) => void;
  selectSpan: (id: string | null) => void;
  selectTask: (id: string | null) => void;
  clearTaskSelection: () => void;
  closeDrawer: () => void;
  // Open the drawer on the Task tab → Trajectory subtab for the current span
  // (used by the `t` keyboard shortcut). Caller passes the target span id,
  // which may be the currently-selected span or a deep-linked one.
  openDrawerOnTrajectory: (spanId: string) => void;
  consumeDrawerRequestedTab: () => void;
  setNavSection: (s: NavSection) => void;
  toggleNavRail: () => void;
  openSessionPicker: () => void;
  closeSessionPicker: () => void;
  toggleHelp: () => void;
  closeHelp: () => void;
  openLegend: () => void;
  closeLegend: () => void;
  toggleLegend: () => void;
  setFocusedAgent: (id: string | null) => void;
  toggleAgentHidden: (id: string) => void;
  showAllAgents: () => void;
  agentsPaused: boolean;
  pausedAt: number | null;
  setAgentsPaused: (paused: boolean, ts?: number) => void;
  zoomIn: () => void;
  zoomOut: () => void;
  setZoom: (sec: number) => void;
  jumpToLive: () => void;
  toggleLiveFollow: () => void;
  setActiveRenderer: (r: GanttRenderer | null) => void;

  // Task-plan rendering preferences (persisted to localStorage).
  taskPlanMode: TaskPlanMode;
  taskPlanVisible: boolean;
  taskPlanView: TaskPlanView;
  setTaskPlanMode: (m: TaskPlanMode) => void;
  toggleTaskPlanVisible: () => void;
  setTaskPlanView: (v: TaskPlanView) => void;

  // Context-window overlay: per-agent sparkline band drawn under spans.
  // Default on, persisted to localStorage.
  contextOverlayVisible: boolean;
  toggleContextOverlayVisible: () => void;

  // Intervention bands overlay: translucent vertical columns on the Gantt
  // canvas at each intervention's atMs (STEER / drift / goldfive). Default
  // on, persisted to localStorage.
  interventionBandsVisible: boolean;
  toggleInterventionBandsVisible: () => void;

  // TrajectoryView legacy-layout escape hatch. `false` (default) renders the
  // new compact layout (unified ribbon + floating drawer + full-width DAG).
  // `true` additionally renders the old stacked REVISIONS / REV N / INTERVENTIONS
  // sections underneath, for users who want the pre-restructure view back.
  trajectoryLegacyExpanded: boolean;
  toggleTrajectoryLegacyExpanded: () => void;

  // Plan-revision selection shared by the Trajectory view and the Gantt's
  // plan subview. `null` means "latest" (cumulative, no pin). A concrete
  // integer pins both surfaces to that revision — the Trajectory view
  // filters its cumulative DAG, and the Gantt plan subview mirrors the
  // same selection (read-only from its side). Single source of truth.
  selectedRevision: number | null;
  setSelectedRevision: (rev: number | null) => void;

  // Sequence diagram (GraphView) zoom + pan state. `null` means "no explicit
  // viewport saved yet" — the view will fit-to-content on mount. Persisted to
  // localStorage so the viewport survives reloads and session switches.
  graphViewport: GraphViewport | null;
  setGraphViewport: (vp: GraphViewport | null) => void;

  // Imperative handles the GraphView registers on mount so global keyboard
  // shortcuts (Ctrl +/-/0) can drive the zoom without reaching into the
  // component. Null when GraphView is not mounted.
  graphActions: GraphActions | null;
  setGraphActions: (a: GraphActions | null) => void;
}

export interface GraphActions {
  zoomIn: () => void;
  zoomOut: () => void;
  zoomReset: () => void;
  fitContent: () => void;
  fitSelection: () => void;
}

const ZOOM_STEP = 1.5;

const ZOOM_MIN = 30;
const ZOOM_MAX = 6 * 60 * 60;

export const useUiStore = create<UiState>((set) => ({
  currentSessionId: null,
  selectedSpanId: null,
  selectedTaskId: null,
  drawerOpen: false,
  drawerRequestedTab: null,
  drawerRequestedTaskSubtab: null,
  navSection: 'sessions',
  navRailOpen: true,
  sessionPickerOpen: false,
  helpOpen: false,
  legendOpen: false,
  focusedAgentId: null,
  hiddenAgentIds: new Set<string>(),
  zoomSeconds: 300,
  liveFollow: true,
  activeRenderer: null,
  liveActivityCollapsed: false,
  toggleLiveActivity: () =>
    set((s) => ({ liveActivityCollapsed: !s.liveActivityCollapsed })),
  agentsPaused: false,
  pausedAt: null,

  setCurrentSession: (id) => set({ currentSessionId: id }),
  selectSpan: (id) => set({ selectedSpanId: id, drawerOpen: id !== null }),
  selectTask: (id) => set({ selectedTaskId: id }),
  clearTaskSelection: () => set({ selectedTaskId: null }),
  closeDrawer: () =>
    set({
      drawerOpen: false,
      selectedSpanId: null,
      selectedTaskId: null,
      drawerRequestedTab: null,
      drawerRequestedTaskSubtab: null,
    }),
  openDrawerOnTrajectory: (spanId) =>
    set({
      selectedSpanId: spanId,
      drawerOpen: true,
      drawerRequestedTab: 'task',
      drawerRequestedTaskSubtab: 'trajectory',
    }),
  consumeDrawerRequestedTab: () =>
    set({ drawerRequestedTab: null, drawerRequestedTaskSubtab: null }),
  setNavSection: (navSection) => set({ navSection }),
  toggleNavRail: () => set((s) => ({ navRailOpen: !s.navRailOpen })),
  openSessionPicker: () => set({ sessionPickerOpen: true }),
  closeSessionPicker: () => set({ sessionPickerOpen: false }),
  toggleHelp: () => set((s) => ({ helpOpen: !s.helpOpen })),
  closeHelp: () => set({ helpOpen: false }),
  openLegend: () => set({ legendOpen: true }),
  closeLegend: () => set({ legendOpen: false }),
  toggleLegend: () => set((s) => ({ legendOpen: !s.legendOpen })),
  setFocusedAgent: (id) => set({ focusedAgentId: id }),
  toggleAgentHidden: (id) =>
    set((s) => {
      const next = new Set(s.hiddenAgentIds);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      s.activeRenderer?.setHiddenAgents(next);
      return { hiddenAgentIds: next };
    }),
  showAllAgents: () =>
    set((s) => {
      if (s.hiddenAgentIds.size === 0) return {};
      const next = new Set<string>();
      s.activeRenderer?.setHiddenAgents(next);
      return { hiddenAgentIds: next };
    }),
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
  jumpToLive: () =>
    set((s) => {
      s.activeRenderer?.setLiveFollow(true);
      return { liveFollow: true };
    }),
  toggleLiveFollow: () =>
    set((s) => {
      const next = !s.liveFollow;
      s.activeRenderer?.setLiveFollow(next);
      return { liveFollow: next };
    }),
  setActiveRenderer: (r) => set({ activeRenderer: r }),

  taskPlanMode: readTaskPlanMode(),
  taskPlanVisible: readTaskPlanVisible(),
  taskPlanView: readTaskPlanView(),
  graphViewport: readGraphViewport(),
  graphActions: null,
  setGraphActions: (a) => set({ graphActions: a }),
  setGraphViewport: (vp) =>
    set(() => {
      writeGraphViewport(vp);
      return { graphViewport: vp };
    }),
  setTaskPlanMode: (m) =>
    set(() => {
      writeTaskPlanMode(m);
      return { taskPlanMode: m };
    }),
  toggleTaskPlanVisible: () =>
    set((s) => {
      const next = !s.taskPlanVisible;
      writeTaskPlanVisible(next);
      return { taskPlanVisible: next };
    }),
  setTaskPlanView: (v) =>
    set(() => {
      writeTaskPlanView(v);
      return { taskPlanView: v };
    }),

  contextOverlayVisible: readContextOverlayVisible(),
  toggleContextOverlayVisible: () =>
    set((s) => {
      const next = !s.contextOverlayVisible;
      writeContextOverlayVisible(next);
      s.activeRenderer?.setContextOverlayVisible(next);
      return { contextOverlayVisible: next };
    }),

  interventionBandsVisible: readInterventionBandsVisible(),
  toggleInterventionBandsVisible: () =>
    set((s) => {
      const next = !s.interventionBandsVisible;
      writeInterventionBandsVisible(next);
      s.activeRenderer?.setInterventionBandsVisible(next);
      return { interventionBandsVisible: next };
    }),

  trajectoryLegacyExpanded: readTrajectoryLegacyExpanded(),
  toggleTrajectoryLegacyExpanded: () =>
    set((s) => {
      const next = !s.trajectoryLegacyExpanded;
      writeTrajectoryLegacyExpanded(next);
      return { trajectoryLegacyExpanded: next };
    }),

  // Ephemeral — intentionally NOT persisted. A pinned rev is a transient
  // inspection mode, not a preference; reload → back to Latest.
  selectedRevision: null,
  setSelectedRevision: (rev) => set({ selectedRevision: rev }),
  setAgentsPaused: (paused, ts) =>
    set((s) => {
      // When pausing, freeze at the session-relative "now" from the renderer
      // (so bars stop growing). When resuming, unfreeze (null).
      const resolvedPausedAt: number | null = paused
        ? (ts ?? s.activeRenderer?.getNowMs() ?? Date.now())
        : null;
      s.activeRenderer?.freezeAt(resolvedPausedAt);
      if (paused) {
        s.activeRenderer?.setLiveFollow(false);
      }
      return {
        agentsPaused: paused,
        pausedAt: resolvedPausedAt,
        liveFollow: paused ? false : s.liveFollow,
      };
    }),
}));
