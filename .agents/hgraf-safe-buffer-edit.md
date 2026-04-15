---
name: hgraf-safe-buffer-edit
description: Safely edit the client ring buffer and transport worker — preserve drop ordering, thread safety, and reconnect-resume semantics.
---

# hgraf-safe-buffer-edit

## When to use

You're modifying `client/harmonograf_client/buffer.py` or the drain/resume path in `client/harmonograf_client/transport.py`. These two modules are the client's back-pressure relief valve — a subtle bug here silently drops telemetry or stalls the upstream.

## Prerequisites

1. Read `buffer.py:1-34` module docstring — the tiered drop policy is specified there: `updates → payload chunks → whole spans`. This ordering is a contract with `docs/design/01 §4.5`, not an implementation detail.
2. Read `buffer.py:45-80` — `EnvelopeKind`, `SpanEnvelope`, `BufferStats`. The buffer is deliberately protobuf-agnostic; it holds opaque payloads.
3. Read `buffer.py` `EventRingBuffer.push` — the push/overflow routine. Look for the `_drop_oldest_update → _strip_oldest_payload_ref → _drop_oldest_span` fallback chain.
4. Read `buffer.py` `PayloadBuffer` — content-addressed blobs keyed by digest. Oldest-first eviction on byte budget overrun.
5. Read `transport.py` drain loop (grep for `pop_batch`) — the worker that moves envelopes from the buffer onto the gRPC stream. Understand when it acks and when it retains.
6. Read `transport.py:649 _dispatch_control` and `:700 _control_kind_name` — control acks flow back up through `StreamTelemetry`, so the buffer must not block the control handler.

## Invariants you must not break

### 1. Drop ordering is a contract, not an optimization

`updates → payload refs → whole spans`. The reason: updates are lossy (the next update carries cumulative state), payload refs are recoverable (the blob stays in PayloadBuffer until byte pressure), whole spans lose observability. Reversing the order quietly degrades the UI — the Gantt bar still renders but the inside of the span goes blank.

If you need a new envelope kind, decide its eviction tier explicitly and document it in the module docstring. Don't leave the order implicit in push() branches.

### 2. `push` never blocks

`buffer.py:26` — "non-blocking: `push` never waits". The instrumentation callers run inside ADK callbacks on the agent's hot path. A `push` that blocks on a lock held by the drain worker deadlocks the agent. Use the existing `threading.Lock` with short critical sections; never `await` inside `push`.

### 3. Counters must be monotonic

`BufferStats.dropped_updates / dropped_payload_refs / dropped_spans` are reported via `Heartbeat.buffered_events` and protocol metrics. They must be monotonic: resetting them on reconnect breaks the rate computation on the server side. If you need "since-last-flush" counts, derive them from monotonic readings on the consumer side.

### 4. Happens-before: SpanStart before SpanUpdate before SpanEnd

The buffer doesn't enforce this — the caller does — but the drop policy does: a lone `SpanUpdate` whose parent `SpanStart` was already drained is valid; a `SpanEnd` without a preceding `SpanStart` is a bug on the sender side. If you change eviction to drop `SpanStart` before its `SpanEnd`, the server will see an end-without-start and log a protocol violation. Keep the invariant: **never drop a SpanStart if its matching SpanEnd is still in the buffer.**

### 5. Payload refs and blob storage must stay in sync

When `_strip_oldest_payload_ref` runs, the envelope's `has_payload_ref` flag flips to `False` but the blob in `PayloadBuffer` remains. The transport layer relies on `has_payload_ref` to decide whether to upload. If you add a new payload owner, update both the ref flag and the blob eviction atomically.

### 6. Control acks ride upstream on the same stream

`proto/harmonograf/v1/types.proto:387-390` — control delivery is full-duplex. The transport's stream-handler reads control downstream and writes acks upstream multiplexed with telemetry. If your buffer change makes telemetry the exclusive writer, control acks stall. Rule: **never gate control acks on buffer drain progress**.

## Step-by-step recipes

### Adding a new envelope kind

1. Add it to `EnvelopeKind` (buffer.py:45).
2. Decide its eviction tier: update-like (drop first), payload-like (strip ref), span-like (drop whole).
3. Update `_drop_oldest_*` methods to treat the new kind correctly — or confirm the existing fallthrough does the right thing.
4. Update the module docstring to include the new kind in the eviction order.
5. Add a unit test in `client/tests/test_buffer.py` that fills the buffer with only the new kind and asserts eviction order.

### Changing capacity defaults

`EventRingBuffer(capacity=2000)` and `PayloadBuffer(max_bytes=16 * 1024 * 1024)` are the defaults. Both are overridable via `HarmonografClient(...)` kwargs (grep `client.py` for the ctor forwarding). Before changing the default:

- Measure at the design rate (30 events/sec). Double the capacity → double the worst-case memory during network stalls.
- Larger buffer → larger resume window on reconnect, more redelivery on flaky networks.
- Smaller buffer → more drops, less backpressure on the agent.

Ship the change alongside a heartbeat metric regression test.

### Touching the drain worker

`transport.py :: _drain_loop` (grep for `pop_batch`):

- The worker runs in a dedicated thread. Do not `await asyncio.*` inside it — the transport uses a thread-safe queue bridge to the async grpc stub.
- Every `pop_batch` call must be followed by either a successful stream write or a re-push of the envelope for a retry. Losing an envelope between pop and write without counting it as dropped is the worst kind of bug — silent data loss.
- Reconnect replays the buffer head (the unsent envelopes are still in the deque because the worker didn't pop them). Don't "clear on reconnect" — you'll lose the last second of telemetry.

### Resume semantics after disconnect

When the gRPC stream breaks:

1. The worker's next `send` raises.
2. `transport.py` catches, backs off, and reconnects.
3. On reconnect, a new Hello is sent, then drain resumes.
4. Envelopes already acked by the server are NOT in the buffer (they were popped). In-flight envelopes (popped but not written) are lost — this is an accepted small window. Document it in any change to the drain protocol.

### Adding a new drop counter

1. Add the field to `BufferStats`.
2. Increment it inside the lock in the push/evict path.
3. Surface it via `heartbeat.py` `build_heartbeat(stats)` → `Heartbeat` proto field.
4. Add the proto field (see `hgraf-add-proto-field.md` in batch 1).
5. Surface in `ProtocolMetrics` so the CLI `format_protocol_metrics` shows it.
6. Unit test: trigger the drop, assert counter increments, assert heartbeat reports the value.

### Thread safety review checklist

When editing `EventRingBuffer`:

- Every read or write of `self._deque` or `self._stats` must be inside `self._lock`.
- Iteration over the deque must copy under the lock, then process outside.
- Don't expose the deque directly. Return tuples or copies.
- No re-entrancy: if a method calls another method that also takes the lock, you deadlock. Use `_locked` private variants.

## Verification

```bash
uv run pytest client/tests/test_buffer.py -x -q
uv run pytest client/tests/test_transport_mock.py -x -q
uv run pytest client/tests -x -q -k "reconnect or resume or overflow"
```

For load testing:

```bash
uv run python -m harmonograf_client.dev.load_test --rate 100 --duration 30
```

(Grep for a load test script in the client dev tools; if none exists, write one as part of the change.)

## Common pitfalls

- **Silent drop of SpanEnd**: if the buffer is full of SpanUpdates and the caller pushes a SpanEnd, evict an update — don't drop the SpanEnd. The current code does this correctly; don't "optimize" by treating End like Start.
- **Clearing the buffer on reconnect**: the drain worker treats the buffer as authoritative. Clearing loses the pre-disconnect window. If you need to drop stale envelopes (e.g., on session change), do it explicitly with a `drop_session(session_id)` method, not on reconnect.
- **Unlocked counter reads**: `stats.dropped_updates` read without the lock is technically racy. For metrics it's acceptable (monotonic), but for tests use `stats_snapshot()` under the lock.
- **Forgetting `has_payload_ref`**: a span whose payload ref was stripped but `has_payload_ref=True` causes the transport to request a blob that was evicted. The caller sees a missing-blob server error. Always flip the flag when stripping.
- **Pushing protobuf objects directly**: the buffer is deliberately proto-agnostic. If you push a `pb.SpanStart` message, you couple the buffer to the proto module and break the layering. Convert at drain time in `transport.py`.
- **Control acks sharing the drain queue**: control responses must NOT be enqueued through the event buffer. They go through a separate path in `transport.py` that writes directly to the stream. If a refactor consolidates them, control latency becomes a function of buffer depth — an anti-goal.
- **Lock held across gRPC call**: grabbing `self._lock` then calling into the transport within the same critical section deadlocks on reconnect. Drain outside the lock.
