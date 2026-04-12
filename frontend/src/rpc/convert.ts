// Proto → internal conversion. The Gantt renderer uses its own trimmed types
// (frontend/src/gantt/types.ts) so the hot rendering path never touches proto
// runtime objects. This module is the single seam between the two worlds.

import type { Timestamp } from '@bufbuild/protobuf/wkt';
import {
  Framework as PbFramework,
  AgentStatus as PbAgentStatus,
  SpanKind as PbSpanKind,
  SpanStatus as PbSpanStatus,
  Capability as PbCapability,
  LinkRelation as PbLinkRelation,
  AnnotationKind as PbAnnotationKind,
  type Agent as PbAgent,
  type Span as PbSpan,
  type SpanLink as PbSpanLink,
  type AttributeValue as PbAttributeValue,
  type PayloadRef as PbPayloadRef,
  type ErrorInfo as PbErrorInfo,
  type Annotation as PbAnnotation,
} from '../pb/harmonograf/v1/types_pb.js';
import type { Annotation as UiAnnotation } from '../state/annotationStore';
import type {
  Agent as UiAgent,
  Capability as UiCapability,
  LinkRelation as UiLinkRelation,
  Span as UiSpan,
  SpanKind as UiSpanKind,
  SpanLink as UiSpanLink,
  SpanStatus as UiSpanStatus,
  AgentFramework as UiAgentFramework,
  AgentConnection as UiAgentConnection,
  AttributeValue as UiAttributeValue,
  PayloadRef as UiPayloadRef,
  ErrorInfo as UiErrorInfo,
} from '../gantt/types';

function tsToMs(t: Timestamp | undefined): number {
  if (!t) return 0;
  // Timestamp.seconds is bigint, nanos is number.
  return Number(t.seconds) * 1000 + Math.floor(t.nanos / 1_000_000);
}

const SPAN_KIND: Record<PbSpanKind, UiSpanKind> = {
  [PbSpanKind.UNSPECIFIED]: 'CUSTOM',
  [PbSpanKind.INVOCATION]: 'INVOCATION',
  [PbSpanKind.LLM_CALL]: 'LLM_CALL',
  [PbSpanKind.TOOL_CALL]: 'TOOL_CALL',
  [PbSpanKind.USER_MESSAGE]: 'USER_MESSAGE',
  [PbSpanKind.AGENT_MESSAGE]: 'AGENT_MESSAGE',
  [PbSpanKind.TRANSFER]: 'TRANSFER',
  [PbSpanKind.WAIT_FOR_HUMAN]: 'WAIT_FOR_HUMAN',
  [PbSpanKind.PLANNED]: 'PLANNED',
  [PbSpanKind.CUSTOM]: 'CUSTOM',
};

const SPAN_STATUS: Record<PbSpanStatus, UiSpanStatus> = {
  [PbSpanStatus.UNSPECIFIED]: 'PENDING',
  [PbSpanStatus.PENDING]: 'PENDING',
  [PbSpanStatus.RUNNING]: 'RUNNING',
  [PbSpanStatus.COMPLETED]: 'COMPLETED',
  [PbSpanStatus.FAILED]: 'FAILED',
  [PbSpanStatus.CANCELLED]: 'CANCELLED',
  [PbSpanStatus.AWAITING_HUMAN]: 'AWAITING_HUMAN',
};

const FRAMEWORK: Record<PbFramework, UiAgentFramework> = {
  [PbFramework.UNSPECIFIED]: 'UNKNOWN',
  [PbFramework.CUSTOM]: 'CUSTOM',
  [PbFramework.ADK]: 'ADK',
};

const AGENT_STATUS: Record<PbAgentStatus, UiAgentConnection> = {
  [PbAgentStatus.UNSPECIFIED]: 'DISCONNECTED',
  [PbAgentStatus.CONNECTED]: 'CONNECTED',
  [PbAgentStatus.DISCONNECTED]: 'DISCONNECTED',
  [PbAgentStatus.CRASHED]: 'CRASHED',
};

const CAPABILITY: Record<PbCapability, UiCapability | null> = {
  [PbCapability.UNSPECIFIED]: null,
  [PbCapability.PAUSE_RESUME]: 'PAUSE_RESUME',
  [PbCapability.CANCEL]: 'CANCEL',
  [PbCapability.REWIND]: 'REWIND',
  [PbCapability.STEERING]: 'STEERING',
  [PbCapability.HUMAN_IN_LOOP]: 'HUMAN_IN_LOOP',
  [PbCapability.INTERCEPT_TRANSFER]: 'INTERCEPT_TRANSFER',
};

const LINK_RELATION: Record<PbLinkRelation, UiLinkRelation> = {
  [PbLinkRelation.UNSPECIFIED]: 'FOLLOWS',
  [PbLinkRelation.INVOKED]: 'INVOKED',
  [PbLinkRelation.WAITING_ON]: 'WAITING_ON',
  [PbLinkRelation.TRIGGERED_BY]: 'TRIGGERED_BY',
  [PbLinkRelation.FOLLOWS]: 'FOLLOWS',
  [PbLinkRelation.REPLACES]: 'REPLACES',
};

export interface SessionOrigin {
  // Wall-clock ms of the session start. Used to convert proto absolute times
  // into session-relative ms for the renderer.
  startMs: number;
}

export function convertAgent(a: PbAgent): UiAgent {
  return {
    id: a.id,
    name: a.name,
    framework: FRAMEWORK[a.framework] ?? 'UNKNOWN',
    capabilities: a.capabilities
      .map((c) => CAPABILITY[c])
      .filter((c): c is UiCapability => c !== null),
    status: AGENT_STATUS[a.status] ?? 'DISCONNECTED',
    connectedAtMs: tsToMs(a.connectedAt),
    currentActivity: '',
    stuck: false,
  };
}

export function convertLink(l: PbSpanLink): UiSpanLink {
  return {
    targetSpanId: l.targetSpanId,
    targetAgentId: l.targetAgentId,
    relation: LINK_RELATION[l.relation] ?? 'FOLLOWS',
  };
}

export function convertAttribute(a: PbAttributeValue): UiAttributeValue {
  switch (a.value.case) {
    case 'stringValue':
      return { kind: 'string', value: a.value.value };
    case 'intValue':
      return { kind: 'int', value: a.value.value };
    case 'doubleValue':
      return { kind: 'double', value: a.value.value };
    case 'boolValue':
      return { kind: 'bool', value: a.value.value };
    case 'bytesValue':
      return { kind: 'bytes', value: a.value.value };
    case 'arrayValue':
      return { kind: 'array', value: a.value.value.values.map(convertAttribute) };
    default:
      return { kind: 'string', value: '' };
  }
}

export function convertPayloadRef(p: PbPayloadRef): UiPayloadRef {
  return {
    digest: p.digest,
    size: Number(p.size),
    mime: p.mime,
    summary: p.summary,
    role: p.role,
    evicted: p.evicted,
  };
}

export function convertError(e: PbErrorInfo): UiErrorInfo {
  return { message: e.message, type: e.type, stack: e.stack };
}

export function convertAnnotation(
  a: PbAnnotation,
  origin: SessionOrigin,
  spanStartMs?: number,
): UiAnnotation {
  const kind =
    a.kind === PbAnnotationKind.STEERING
      ? 'STEERING'
      : a.kind === PbAnnotationKind.HUMAN_RESPONSE
        ? 'HUMAN_RESPONSE'
        : 'COMMENT';
  let spanId: string | null = null;
  let agentId: string | null = null;
  let atMs = spanStartMs ?? 0;
  const target = a.target?.target;
  if (target?.case === 'spanId') {
    spanId = target.value;
  } else if (target?.case === 'agentTime') {
    agentId = target.value.agentId;
    if (target.value.at) {
      atMs = tsToMs(target.value.at) - origin.startMs;
    }
  }
  const createdAbs = a.createdAt ? tsToMs(a.createdAt) : 0;
  const deliveredAbs = a.deliveredAt ? tsToMs(a.deliveredAt) : null;
  return {
    id: a.id,
    sessionId: a.sessionId,
    spanId,
    agentId,
    atMs,
    author: a.author,
    kind,
    body: a.body,
    createdAtMs: createdAbs,
    deliveredAtMs: deliveredAbs,
    pending: false,
    error: null,
  };
}

export function convertSpan(s: PbSpan, origin: SessionOrigin): UiSpan {
  const startAbs = tsToMs(s.startTime);
  const endAbs = s.endTime ? tsToMs(s.endTime) : null;
  const attributes: Record<string, UiAttributeValue> = {};
  for (const [k, v] of Object.entries(s.attributes)) {
    attributes[k] = convertAttribute(v);
  }
  return {
    id: s.id,
    sessionId: s.sessionId,
    agentId: s.agentId,
    parentSpanId: s.parentSpanId || null,
    kind: SPAN_KIND[s.kind] ?? 'CUSTOM',
    status: SPAN_STATUS[s.status] ?? 'PENDING',
    name: s.name,
    startMs: startAbs - origin.startMs,
    endMs: endAbs === null ? null : endAbs - origin.startMs,
    links: s.links.map(convertLink),
    attributes,
    payloadRefs: s.payloadRefs.map(convertPayloadRef),
    error: s.error ? convertError(s.error) : null,
    lane: -1,
    replaced: false,
  };
}
