import { describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { TimestampSchema } from '@bufbuild/protobuf/wkt';
import { SessionStore } from '../../gantt/index';
import { applyUserMessage } from '../../rpc/goldfiveEvent';
import { UserMessageReceivedSchema } from '../../pb/harmonograf/v1/telemetry_pb';
import { deriveInterventionsFromStore } from '../../lib/interventions';
import { USER_ACTOR_ID } from '../../theme/agentColors';

// Tests the WatchSession dispatch for the new UserMessageReceived oneof
// variant (harmonograf user-message UX gap). Coverage:
//   * record ingestion onto store.userMessages,
//   * synthesized USER_MESSAGE span on the user actor row (so the
//     Gantt user lane and Graph origin lifeline render the marker),
//   * user actor row registration via ensureSyntheticActor,
//   * mid-turn flag carries through to the synthesized span attribute,
//   * intervention deriver surfaces the row with source='user' and
//     a USER_MESSAGE / USER_MESSAGE_INTERJECTION kind,
//   * dedup on (runId, content head, recordedAtMs) so reconnect replay
//     doesn't double-render the marker.

function mkUserMessagePb(
  over: Partial<{
    runId: string;
    sequence: bigint;
    sessionId: string;
    content: string;
    author: string;
    midTurn: boolean;
    invocationId: string;
    emittedAtSecs: number;
    emittedAtNanos: number;
  }> = {},
) {
  const emittedAt =
    over.emittedAtSecs != null
      ? create(TimestampSchema, {
          seconds: BigInt(over.emittedAtSecs),
          nanos: over.emittedAtNanos ?? 0,
        })
      : undefined;
  return create(UserMessageReceivedSchema, {
    runId: over.runId ?? 'run-1',
    sequence: over.sequence ?? 7n,
    sessionId: over.sessionId ?? 'sess-um',
    content:
      over.content ?? 'forget solar panels. tell me about solar flares.',
    author: over.author ?? 'alice',
    midTurn: over.midTurn ?? false,
    invocationId: over.invocationId ?? '',
    emittedAt,
  });
}

describe('applyUserMessage', () => {
  it('appends a record onto the user-message registry', () => {
    const store = new SessionStore();
    applyUserMessage(mkUserMessagePb({ emittedAtSecs: 100 }), store, 50_000);
    const list = store.userMessages.list();
    expect(list).toHaveLength(1);
    expect(list[0].content).toBe(
      'forget solar panels. tell me about solar flares.',
    );
    expect(list[0].author).toBe('alice');
    expect(list[0].midTurn).toBe(false);
    // emittedAt is 100s = 100_000ms; sessionStart=50_000 ⇒ relative=50_000
    expect(list[0].recordedAtMs).toBe(50_000);
  });

  it('synthesizes a USER_MESSAGE span on the user actor lane', () => {
    const store = new SessionStore();
    applyUserMessage(
      mkUserMessagePb({ emittedAtSecs: 100, content: 'hi there' }),
      store,
      50_000,
    );
    const userSpans = [...store.spans.queryAgent(
      USER_ACTOR_ID,
      0,
      Number.POSITIVE_INFINITY,
    )];
    expect(userSpans.length).toBe(1);
    expect(userSpans[0].kind).toBe('USER_MESSAGE');
    expect(userSpans[0].name).toBe('hi there');
    expect(userSpans[0].attributes['user.content']).toEqual({
      kind: 'string',
      value: 'hi there',
    });
    expect(userSpans[0].attributes['harmonograf.user_message_marker']).toEqual({
      kind: 'bool',
      value: true,
    });
  });

  it('registers the user actor row even on a fresh store', () => {
    const store = new SessionStore();
    applyUserMessage(mkUserMessagePb(), store, 0);
    const userActor = store.agents.get(USER_ACTOR_ID);
    expect(userActor).toBeDefined();
    expect(userActor?.name).toBe('user');
  });

  it('carries the mid_turn flag onto the synthesized span attribute', () => {
    const store = new SessionStore();
    applyUserMessage(
      mkUserMessagePb({
        midTurn: true,
        invocationId: 'inv-9',
        content: 'interject!',
      }),
      store,
      0,
    );
    const userSpans = [...store.spans.queryAgent(
      USER_ACTOR_ID,
      0,
      Number.POSITIVE_INFINITY,
    )];
    expect(userSpans[0].attributes['user.mid_turn']).toEqual({
      kind: 'bool',
      value: true,
    });
    expect(userSpans[0].attributes['user.invocation_id']).toEqual({
      kind: 'string',
      value: 'inv-9',
    });
  });

  it('dedups exact-replay messages by content + timestamp', () => {
    const store = new SessionStore();
    const pb = mkUserMessagePb({ emittedAtSecs: 100, content: 'same' });
    applyUserMessage(pb, store, 0);
    applyUserMessage(pb, store, 0);
    expect(store.userMessages.list()).toHaveLength(1);
  });

  it('produces an intervention row with source=user', () => {
    const store = new SessionStore();
    applyUserMessage(
      mkUserMessagePb({
        emittedAtSecs: 100,
        content: 'forget solar panels. tell me about solar flares.',
      }),
      store,
      50_000,
    );
    const rows = deriveInterventionsFromStore(store, []);
    const userRows = rows.filter((r) => r.source === 'user');
    expect(userRows).toHaveLength(1);
    expect(userRows[0].kind).toBe('USER_MESSAGE');
    expect(userRows[0].bodyOrReason).toBe(
      'forget solar panels. tell me about solar flares.',
    );
    expect(userRows[0].author).toBe('alice');
  });

  it('renders mid-turn rows with a distinct USER_MESSAGE_INTERJECTION kind', () => {
    const store = new SessionStore();
    applyUserMessage(
      mkUserMessagePb({
        emittedAtSecs: 100,
        midTurn: true,
        content: 'interject!',
      }),
      store,
      50_000,
    );
    const rows = deriveInterventionsFromStore(store, []);
    const r = rows.find((row) => row.source === 'user');
    expect(r?.kind).toBe('USER_MESSAGE_INTERJECTION');
  });
});
