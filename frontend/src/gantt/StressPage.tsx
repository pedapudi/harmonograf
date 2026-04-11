import { useEffect, useMemo, useRef, useState } from 'react';
import { SessionStore } from './index';
import { GanttRenderer } from './renderer';
import { runAllScenarios, type ScenarioResult } from './stress';

// Dev-only /stress page. Instantiates its own SessionStore and runs the four
// scenarios from doc 04 §9.4, displaying pass/fail vs. the hard budgets.
export function StressPage() {
  const store = useMemo(() => new SessionStore(), []);
  // We reach into the active renderer by swapping out the constructor callback
  // — simpler than adding a context. The harness below reads it via this ref.
  const rendererRef = useRef<GanttRenderer | null>(null);
  const [results, setResults] = useState<ScenarioResult[]>([]);
  const [running, setRunning] = useState(false);

  // Capture the renderer instance created by GanttCanvas. We do that by
  // monkey-patching attach/detach via a custom hook — but simpler: we construct
  // a sentinel store and walk the DOM after mount. The cleanest path is to
  // attach our own renderer alongside the component. Instead, we create a
  // dedicated renderer here and let GanttCanvas manage only DOM; but that
  // would run two loops. For the stress tool, we take a different approach:
  // construct the renderer ourselves.
  useEffect(() => {
    // Initial nowMs so the now-cursor is drawn at position 0.
    store.nowMs = 0;
  }, [store]);

  const run = async () => {
    const renderer = rendererRef.current;
    if (!renderer) {
      alert('Renderer not mounted yet — wait a tick.');
      return;
    }
    setRunning(true);
    setResults([]);
    const out: ScenarioResult[] = [];
    for await (const r of iterScenarios({ store, renderer })) {
      out.push(r);
      setResults([...out]);
    }
    setRunning(false);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <header
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid var(--md-sys-color-outline-variant, #43474e)',
          display: 'flex',
          alignItems: 'center',
          gap: 16,
        }}
      >
        <h1 style={{ margin: 0, fontSize: 18 }}>Gantt Stress Harness</h1>
        <button
          onClick={run}
          disabled={running}
          style={{
            padding: '8px 16px',
            borderRadius: 999,
            border: 'none',
            background: 'var(--md-sys-color-primary, #a8c8ff)',
            color: 'var(--md-sys-color-on-primary, #003062)',
            cursor: running ? 'not-allowed' : 'pointer',
            fontWeight: 600,
          }}
        >
          {running ? 'Running…' : 'Run all scenarios'}
        </button>
        <a
          href="#/"
          style={{ marginLeft: 'auto', color: 'var(--md-sys-color-primary)' }}
        >
          ← back to app
        </a>
      </header>
      <div style={{ flex: 1, minHeight: 0 }}>
        <GanttCanvasWithRendererRef store={store} rendererRef={rendererRef} />
      </div>
      <div
        style={{
          borderTop: '1px solid var(--md-sys-color-outline-variant, #43474e)',
          padding: 12,
          maxHeight: '40%',
          overflow: 'auto',
          background: 'var(--md-sys-color-surface-container, #1c1f26)',
        }}
      >
        {results.length === 0 && !running && (
          <div style={{ opacity: 0.7 }}>No results yet. Click "Run all scenarios".</div>
        )}
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ textAlign: 'left', opacity: 0.7 }}>
              <th>Scenario</th>
              <th>Spans</th>
              <th>Avg frame ms</th>
              <th>p95 frame ms</th>
              <th>Budget</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {results.map((r) => (
              <tr
                key={r.name}
                style={{
                  borderTop: '1px solid var(--md-sys-color-outline-variant, #43474e)',
                }}
              >
                <td style={{ padding: '6px 4px' }}>
                  <strong>{r.name}</strong>
                  <div style={{ opacity: 0.7, fontSize: 11 }}>{r.description}</div>
                </td>
                <td>{r.spanCount.toLocaleString()}</td>
                <td>{r.avgFrameMs.toFixed(2)}</td>
                <td>{r.p95FrameMs.toFixed(2)}</td>
                <td>{r.budgetFrameMs} ms</td>
                <td
                  style={{
                    color: r.pass
                      ? 'var(--md-sys-color-secondary, #7fdba0)'
                      : 'var(--md-sys-color-error, #ffb4ab)',
                    fontWeight: 600,
                  }}
                >
                  {r.pass ? 'PASS' : 'FAIL'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

async function* iterScenarios(ctx: {
  store: SessionStore;
  renderer: GanttRenderer;
}): AsyncGenerator<ScenarioResult> {
  const results = await runAllScenarios(ctx);
  for (const r of results) yield r;
}

// Small wrapper that exposes the GanttRenderer instance to the parent via a
// ref. The main GanttCanvas doesn't need this; only the stress page does, so
// we keep it local.
function GanttCanvasWithRendererRef({
  store,
  rendererRef,
}: {
  store: SessionStore;
  rendererRef: React.MutableRefObject<GanttRenderer | null>;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const bgRef = useRef<HTMLCanvasElement | null>(null);
  const blocksRef = useRef<HTMLCanvasElement | null>(null);
  const overlayRef = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const bg = bgRef.current;
    const blocks = blocksRef.current;
    const overlay = overlayRef.current;
    const container = containerRef.current;
    if (!bg || !blocks || !overlay || !container) return;
    const r = new GanttRenderer(store);
    rendererRef.current = r;
    r.attach(bg, blocks, overlay);
    const ro = new ResizeObserver(() => {
      const rect = container.getBoundingClientRect();
      r.resize(rect.width, rect.height, window.devicePixelRatio || 1);
    });
    ro.observe(container);
    const rect = container.getBoundingClientRect();
    r.resize(rect.width, rect.height, window.devicePixelRatio || 1);
    return () => {
      ro.disconnect();
      r.detach();
      rendererRef.current = null;
    };
  }, [store, rendererRef]);
  return (
    <div
      ref={containerRef}
      style={{
        position: 'relative',
        width: '100%',
        height: '100%',
        background: 'var(--md-sys-color-surface, #10131a)',
      }}
    >
      <canvas ref={bgRef} style={layerStyle(0)} />
      <canvas ref={blocksRef} style={layerStyle(1)} />
      <canvas ref={overlayRef} style={layerStyle(2)} />
    </div>
  );
}

function layerStyle(z: number): React.CSSProperties {
  return {
    position: 'absolute',
    inset: 0,
    width: '100%',
    height: '100%',
    zIndex: z,
  };
}
