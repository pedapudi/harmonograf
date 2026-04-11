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

export type AgentFramework = 'ADK' | 'CUSTOM' | 'UNKNOWN';
export type AgentConnection = 'CONNECTED' | 'DISCONNECTED' | 'CRASHED';

export interface Agent {
  id: string;
  name: string;
  framework: AgentFramework;
  capabilities: Capability[];
  status: AgentConnection;
  connectedAtMs: number;
}
