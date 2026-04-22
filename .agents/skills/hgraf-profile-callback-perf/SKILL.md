---
name: hgraf-profile-callback-perf
description: Profile ADK callback overhead on the current telemetry plugin + goldfive per-LLM-call metrics.
---

# hgraf-profile-callback-perf

## When to use

You changed something in the client's ADK plugin
(`client/harmonograf_client/telemetry_plugin.py`), in the ring buffer
or transport (`buffer.py` / `transport.py`), or in the server's
ingest pipeline, and you want to confirm you didn't regress per-call
latency.

## Where the hot paths are now

With goldfive owning orchestration, the client's remaining hot paths
are short:

| Hot path | Location | Dominant cost |
|---|---|---|
| ADK callback → span enqueue | `telemetry_plugin.py` | `emit_span_*` marshal + ring push |
| Ring buffer push | `buffer.py` | One deque append under `threading.Lock` |
| Transport drain | `transport.py::_send_loop` | `pop_batch` + proto marshal + gRPC queue put |
| Server ingest | `server/ingest.py::handle_message` | `pb_span_to_storage` + `store.upsert_span` + `bus.publish_*` |
| Sqlite write | `storage/sqlite.py::append_span` | aiosqlite INSERT under process-wide asyncio.Lock |
| Bus fan-out | `bus.py::publish` | One `put_nowait` per subscriber |

See `dev-guide/performance-tuning.md` for the full map.

## The three metrics that matter most

1. **`goldfive.llm.duration_ms`** (goldfive#172) — wall-clock time of
   each LLM call. Almost always the dominant cost in a "slow agent"
   complaint.
2. **`goldfive.llm.request.chars`** — request prompt size. Balloons
   post-STEER when the steerer injects drift body + task state.
3. **`buffered_events`** (from `Heartbeat`) — if this climbs, the
   client can't drain fast enough. Usually the signal is network,
   not local CPU.

Log these via goldfive at INFO to see trends.

## Sampling the plugin

```bash
# Attach py-spy to a running agent:
py-spy top --pid $(pgrep -f my_agent)
py-spy dump --pid $(pgrep -f my_agent) | head -60
```

Hot frames to expect on a healthy agent:

- `google.adk.*` (ADK machinery)
- `goldfive.*` (planner, steerer, detectors)
- `litellm.*` or the model provider's client
- Minimal time in `harmonograf_client.*`

Hot frames on `harmonograf_client.telemetry_plugin` or
`harmonograf_client.transport` that exceed single-digit percentage
of CPU point at a regression in the plugin or the transport.

## Common regression shapes

1. **Large payload hashing.** A tool or LLM output in the tens of MB
   pays a `hashlib.sha256` + chunking cost on every emit. Look for
   hot frames in `client.py::_attach_payload` or
   `transport.py::_drain_payloads`.
2. **Attribute dict copies.** The `_stamp_agent_attrs` path copies
   the attrs dict on first sight of each agent. That cost is bounded
   (once per `(session_id, agent_id)` pair) but if your change
   invalidates the cache more aggressively, it multiplies.
3. **Duplicate plugin (#68).** Silent duplicates don't cost much on
   callbacks (they short-circuit early), but the initial dedup walk
   is O(plugins). Not an issue in practice but worth a glance.

## Cross-links

- `dev-guide/performance-tuning.md` — the complete hot-path map.
- `runbooks/high-latency-callbacks.md` — operator-level diagnosis.
- goldfive docs for planner / detector profiling.
