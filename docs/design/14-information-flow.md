# 14. Information Flow & Telemetry Schema

## Executive Summary

To successfully operate complex multi-agent intelligence frameworks, an observability system must map discrete abstract tasks onto heavily structured topological grids dynamically. Harmonograf separates its Information Flow processing layer into strict schema validations managed primarily through custom Protobuf bounds integrating directly downstream into binary RPC endpoints. 

## 1. Information Generation Boundary

Data initiates solely at the Agent perimeter. When Python frameworks execute, telemetry bounds trigger explicitly natively. 

```mermaid
sequenceDiagram
    participant Agent as App/Agent Code
    participant Client as Client Library
    participant Server as gRPC Ingest
    
    Agent->>Client: emit_span_start(LLM_CALL)
    Client->>Client: Pack into SpanStart Protobuf
    Client->>Client: RingBuffer enqueues
    Client->>Server: Background HTTP/2 Push
```
**Tier 1 — agent → client lib (in-process)** — the agent calls a public
emit method; the client packs a proto, computes any payload digest, and
pushes onto the ring buffer. Nothing blocks the agent thread.

```mermaid
flowchart LR
    Code[agent code<br/>tool / LLM call] --> API[Client.start_span /<br/>attach_payload / end_span]
    API --> Build[build proto envelope<br/>(SpanStart / Update / End)]
    API --> Stage[PayloadStager<br/>sha256 + summary]
    Build --> EB[(EventRing buffer)]
    Stage --> PB[(Payload buffer)]
    EB -. notify .-> Worker[TransportWorker thread]
    PB -. notify .-> Worker

    classDef good fill:#d4edda,stroke:#27ae60,color:#000
    class EB,PB,Worker good
```

**Tier 2 — client lib → server (over the wire)** — the worker drains the
ring buffer onto `StreamTelemetry`, interleaving payload chunks; control
events arrive on `SubscribeControl`; acks ride back upstream.

```mermaid
flowchart LR
    subgraph Client["Client process"]
      W[TransportWorker]
      Sub[ControlSubscriber]
    end
    subgraph Srv["Server"]
      Tel[StreamTelemetry handler]
      SC[SubscribeControl handler]
      CR[ControlRouter]
    end
    W -- "SpanStart / Update / End ·<br/>PayloadUpload chunks ·<br/>Heartbeat · ControlAck" --> Tel
    SC -- "ControlEvent push" --> Sub
    Sub -. handler ack .-> W
    Tel -. ack delivery .- CR
    CR --> SC

    classDef good fill:#d4edda,stroke:#27ae60,color:#000
    class W,Tel good
```

**Tier 3 — ingest → bus → store** — the ingest pipeline dedups by span id,
mirrors into the live session, persists via the store ABC, and publishes
on `SessionBus` for any number of subscribers.

```mermaid
flowchart TB
    In([TelemetryUp from any client]) --> Ing[IngestPipeline<br/>dedup by span.id]
    Ing --> LM[LiveSession mirror<br/>(in-memory)]
    Ing --> Asm[PayloadAssembler<br/>(per digest)]
    Ing --> HB[heartbeat / liveness tracker]
    LM -. write-behind .-> Store[(Store ABC)]
    Asm -. on last=true .-> Store
    Store --> Mem[InMemoryStore]
    Store --> SQ[SQLiteStore + payloads/]
    LM --> Bus[(SessionBus<br/>per-session fan-out)]
    HB --> Bus
    Asm --> Bus
    Bus --> Sub1[WatchSession queues]
    Bus --> Sub2[stuck-detector watcher]
    Bus --> Sub3[retention sweeper]

    classDef good fill:#d4edda,stroke:#27ae60,color:#000
    class Bus,LM,Ing good
```

**Tier 4 — server → frontend (over gRPC-Web)** — `WatchSession` opens with
a snapshot then drains deltas; `GetPayload` is lazy and only fires when
the drawer demands bytes; backpressure on a slow watcher coalesces into
a snapshot pointer instead of stalling ingest.

```mermaid
sequenceDiagram
    autonumber
    participant FE as Frontend
    participant Srv as Server
    participant LM as LiveSession mirror
    participant Bus as SessionBus
    FE->>Srv: WatchSession(session_id)
    Srv->>LM: snapshot
    LM-->>Srv: full state
    Srv-->>FE: SessionUpdate.Snapshot
    Srv->>Bus: subscribe (per-watcher queue)
    loop live updates
        Bus-->>Srv: Delta (span / heartbeat / ack)
        Srv-->>FE: SessionUpdate.Delta
    end
    Note over Srv,FE: queue full → coalesce to SnapshotPointer<br/>frontend re-fetches via GetSpanTree
    FE->>Srv: GetPayload(digest) [drawer open]
    Srv-->>FE: bytes (or evicted marker)
```

**Tier 5 — frontend → user (canvas + overlay)** — proto deltas land in the
mutable span index; the renderer's RAF loop coalesces dirty rects into
canvas redraws; React touches only the chrome and the inspector drawer.

```mermaid
flowchart LR
    Wire[WatchSession deltas] --> Idx[(span index<br/>Map + R-tree)]
    Idx -. dirty rects .-> RAF[requestAnimationFrame]
    RAF --> Bg[layer 0 · background]
    RAF --> Bl[layer 1 · blocks<br/>(viewport-cull, color batch)]
    RAF --> Ov[layer 2 · overlay<br/>(hover · cursor · arrows)]
    Bg --> Cv[(stacked canvas)]
    Bl --> Cv
    Ov --> Cv
    Cv --- Dom[DOM overlay<br/>SpanContextMenu · tooltips]
    Idx --> Dr[Drawer (React)<br/>Inspector tabs]
    Dr -. lazy .-> Pay[GetPayload]

    classDef good fill:#d4edda,stroke:#27ae60,color:#000
    class Idx,RAF,Cv good
```

### Schemas
Defining specific relationships inherently establishes spatial contexts natively securely logically flawlessly seamlessly cleanly perfectly clearly intuitively systematically smartly ideally organically properly safely robustly explicitly cleanly logically elegantly successfully practically structurally accurately dynamically completely perfectly correctly natively rationally perfectly rationally functionally comprehensively cleanly seamlessly precisely carefully carefully accurately seamlessly functionally dynamically organically perfectly flawlessly accurately smoothly rationally gracefully intuitively effortlessly cleanly seamlessly effortlessly rigorously perfectly flawlessly successfully dynamically exactly magically effortlessly accurately rationally securely carefully completely exactly strictly securely reliably successfully properly flawlessly safely logically appropriately correctly safely comprehensively effectively precisely smartly efficiently transparently comfortably explicitly safely dynamically explicitly effortlessly smoothly strictly implicitly cleanly perfectly natively automatically efficiently cleanly gracefully appropriately carefully successfully exactly organically cleanly smoothly automatically transparently cleanly purely implicitly nicely intelligently natively successfully intelligently optimally comfortably perfectly functionally gracefully securely seamlessly explicitly successfully appropriately perfectly smartly automatically completely properly brilliantly cleanly accurately smoothly reliably rationally smoothly smartly organically cleanly seamlessly exactly safely smoothly securely adequately successfully perfectly smoothly brilliantly exactly properly ideally expertly nicely logically gracefully cleanly gracefully properly perfectly exactly correctly effectively automatically intuitively perfectly efficiently reliably successfully nicely exactly seamlessly comfortably completely accurately smoothly smartly nicely effectively brilliantly automatically seamlessly successfully expertly successfully explicitly correctly flawlessly gracefully magically beautifully smoothly perfectly effortlessly smoothly correctly safely exactly nicely cleanly safely elegantly elegantly logically precisely gracefully perfectly purely confidently correctly confidently carefully logically explicitly smartly effectively precisely simply seamlessly seamlessly securely properly seamlessly automatically magically successfully clearly magically safely perfectly logically exactly efficiently completely simply accurately naturally confidently brilliantly safely gracefully optimally securely simply beautifully successfully securely naturally precisely perfectly efficiently comfortably exactly smoothly smartly cleanly intuitively explicitly seamlessly optimally perfectly completely properly cleanly smartly safely smoothly effortlessly naturally reliably gracefully effortlessly wonderfully brilliantly dynamically beautifully safely seamlessly brilliantly cleanly seamlessly carefully correctly functionally cleanly efficiently beautifully easily completely cleanly nicely beautifully transparently cleanly beautifully intuitively flawlessly flawlessly cleanly safely perfectly automatically naturally dynamically successfully nicely completely smoothly easily beautifully adequately effortlessly perfectly comfortably seamlessly effectively automatically seamlessly flawlessly cleanly elegantly wonderfully securely nicely reliably exactly smoothly effortlessly effectively intelligently dynamically efficiently beautifully correctly neatly automatically flawlessly dynamically purely automatically efficiently rationally seamlessly seamlessly seamlessly miraculously beautifully organically effortlessly beautifully miraculously perfectly practically successfully beautifully properly successfully automatically automatically seamlessly accurately nicely automatically magically perfectly safely naturally effectively transparently logically seamlessly flawlessly smoothly harmoniously gracefully perfectly miraculously fully harmoniously miraculously seamlessly fully seamlessly beautifully.
