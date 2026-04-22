// Wire-independent span types. These mirror the proto shape from doc 01 §2.3
// but carry only the fields the renderer needs — it should never import proto.

export type SpanKind =
  | 'INVOCATION'
  | 'LLM_CALL'
  | 'TOOL_CALL'
  | 'USER_MESSAGE'
  | 'AGENT_MESSAGE'
  | 'TRANSFER'
  | 'WAIT_FOR_HUMAN'
  | 'PLANNED'
  | 'CUSTOM';

export type SpanStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED'
  | 'AWAITING_HUMAN';

export type LinkRelation =
  | 'INVOKED'
  | 'WAITING_ON'
  | 'TRIGGERED_BY'
  | 'FOLLOWS'
  | 'REPLACES';

export interface SpanLink {
  targetSpanId: string;
  targetAgentId: string;
  relation: LinkRelation;
}

export type AttributeValue =
  | { kind: 'string'; value: string }
  | { kind: 'int'; value: bigint }
  | { kind: 'double'; value: number }
  | { kind: 'bool'; value: boolean }
  | { kind: 'bytes'; value: Uint8Array }
  | { kind: 'array'; value: AttributeValue[] };

export interface PayloadRef {
  digest: string;
  size: number;
  mime: string;
  summary: string;
  role: string;
  evicted: boolean;
}

export interface ErrorInfo {
  message: string;
  type: string;
  stack: string;
}

export interface Span {
  id: string;
  sessionId: string;
  agentId: string;
  parentSpanId: string | null;
  kind: SpanKind;
  status: SpanStatus;
  name: string;
  // Milliseconds since session start. Using relative time keeps arithmetic cheap
  // and matches how the Gantt renders (x = (t - viewportStart) * pxPerMs).
  startMs: number;
  endMs: number | null; // null while RUNNING
  links: SpanLink[];
  attributes: Record<string, AttributeValue>;
  payloadRefs: PayloadRef[];
  error: ErrorInfo | null;
  // Lane within the agent row assigned at layout time. -1 means unassigned.
  lane: number;
  // True if this span was replaced by another (REPLACES link). Renderer dims it.
  replaced: boolean;
}

export type Capability =
  | 'PAUSE_RESUME'
  | 'CANCEL'
  | 'REWIND'
  | 'STEERING'
  | 'HUMAN_IN_LOOP'
  | 'INTERCEPT_TRANSFER';

// ── Task plan types (mirror harmonograf.v1.TaskStatus / Task / TaskPlan) ─────
// Kept as plain TS types so the renderer never imports proto runtime objects.

// BLOCKED is goldfive-only (no harmonograf TaskStatus mapping) — goldfive
// emits it when a task is waiting on an external input (tool, another
// agent, a human). The renderer treats it like PENDING visually; chrome
// surfaces it explicitly so operators can tell the task is stalled vs.
// merely not-yet-started.
export type TaskStatus =
  | 'UNSPECIFIED'
  | 'PENDING'
  | 'RUNNING'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED'
  | 'BLOCKED';

export interface Task {
  id: string;
  title: string;
  description: string;
  assigneeAgentId: string;
  status: TaskStatus;
  // Session-relative ms (0 if the planner didn't provide timing).
  predictedStartMs: number;
  predictedDurationMs: number;
  boundSpanId: string;
  // harmonograf#110 / goldfive#205: structured cancel reason stamped on
  // the most recent CANCELLED / FAILED transition. Colon-prefixed tag
  // (upstream_failed / run_aborted / user_cancel / user_steer /
  // superseded_by_revision) + a provenance id or human tail.
  // Empty on PENDING / RUNNING / COMPLETED / BLOCKED tasks. Surfaced in
  // the TaskStagesGraph tooltip, the Drawer Task Overview section, and
  // the TrajectoryView task-delta list.
  cancelReason?: string;
}

export interface TaskEdge {
  fromTaskId: string;
  toTaskId: string;
}

export interface TaskPlan {
  id: string;
  invocationSpanId: string;
  plannerAgentId: string;
  createdAtMs: number; // session-relative
  summary: string;
  tasks: Task[];
  edges: TaskEdge[];
  revisionReason: string;
  revisionKind?: string;
  revisionSeverity?: string;
  revisionIndex?: number;
  // harmonograf#99 / goldfive#199: strict dedup key joining this plan
  // revision to its originating annotation or drift. Non-empty on every
  // revision (user-control = annotation_id, autonomous = DriftEvent.id);
  // empty on the initial plan.
  triggerEventId?: string;
}

// Per-agent context-window telemetry sample (task #3). Converted from the
// wire `ContextWindowSample` at the rpc/convert seam: recordedAt becomes
// session-relative ms, int64 bigints narrow to number. The renderer consumes
// this shape directly and never touches proto runtime objects.
export interface ContextWindowSample {
  // Session-relative ms (matches Span.startMs).
  tMs: number;
  tokens: number;
  limitTokens: number;
}

export type AgentFramework = 'ADK' | 'CUSTOM' | 'UNKNOWN';
export type AgentConnection = 'CONNECTED' | 'DISCONNECTED' | 'CRASHED';

export interface Agent {
  id: string;
  name: string;
  framework: AgentFramework;
  capabilities: Capability[];
  status: AgentConnection;
  connectedAtMs: number;
  currentActivity: string;   // "" if none
  stuck: boolean;
  taskReport: string;        // latest self-reported task description
  taskReportAt: number;      // ms timestamp when report was recorded
  // Free-form key/value metadata copied from the Hello frame. Currently used
  // for `harmonograf.execution_mode` ("sequential" | "parallel" | "delegated")
  // so the chrome can surface which orchestration mode the agent is running.
  metadata: Record<string, string>;
}

// Canonical orchestration modes advertised via
// `agent.metadata['harmonograf.execution_mode']`. Anything else is treated as
// unknown by the UI.
export type ExecutionMode = 'sequential' | 'parallel' | 'delegated';

export const EXECUTION_MODE_KEY = 'harmonograf.execution_mode';

export function readExecutionMode(
  agent: Pick<Agent, 'metadata'> | null | undefined,
): ExecutionMode | null {
  const raw = agent?.metadata?.[EXECUTION_MODE_KEY];
  if (raw === 'sequential' || raw === 'parallel' || raw === 'delegated') {
    return raw;
  }
  return null;
}
