// InstrumentsViewZ.tsx — everything that is NOT the gantt, coalesced into one
// mission-control scroll: THE PLAN LEADS (reel → DAG), then conversation +
// topology side-by-side, then ONE coordinated time-track stack (drift+judge
// seismograph over the time-aligned intervention ladder).
// (compose.html mainHybrid instruments branch 718-735.)

import type { ZSession } from './adapter';
import { Fig } from './Fig';
import { FingerprintZ } from './FingerprintZ';
import { PlanZ } from './PlanZ';
import { SequenceZ } from './SequenceZ';
import { ChordZ } from './ChordZ';
import { SeismographZ } from './SeismographZ';
import { LadderZ } from './LadderZ';

export interface InstrumentsViewZProps {
  z: ZSession;
}

function statusLabel(z: ZSession): string {
  if (z.status === 'live') return 'live';
  if (z.status === 'done') return '✓ done';
  return '✕ failed';
}

export function InstrumentsViewZ({ z }: InstrumentsViewZProps) {
  return (
    <>
      <div className="zk-sess-head">
        <FingerprintZ fp={z.fp} status={z.status} id={z.id} size={58} />
        <div>
          <div className="zk-sess-title">{z.id || 'session'}</div>
          <div style={{ fontSize: 11, color: 'var(--ink-faint)' }}>{z.goal}</div>
        </div>
        <span style={{ flex: 1 }} />
        <span className="zk-sess-stats">
          <span
            className={`dn-pill ${
              z.status === 'live' ? 'accent' : z.status === 'done' ? 'good' : 'bad'
            }`}
          >
            {statusLabel(z)}
          </span>
          <span className="dn-pill flat tnum">{z.spans.length} spans</span>
          <span className="dn-pill flat tnum">{Math.round(z.now)}s</span>
        </span>
      </div>

      <Fig>{(w) => <PlanZ z={z} W={w} />}</Fig>

      {/* Sequence — FULL WIDTH so the lifelines spread out and the diagram reads
          cleanly (it was too crowded in the half-width pane). */}
      <div className="gantt-click">
        <h3>sequence — who said what to whom, when</h3>
        <Fig fallback={940}>{(w) => <SequenceZ z={z} W={w} H={420} />}</Fig>
      </div>

      {/* Topology — FULL WIDTH row; the chord itself stays clamped to ~460 and
          centred (see note below). */}
      <h3>topology — who initiates, who receives</h3>
      {/* Clamp the chord's LOGICAL size AND its rendered width to the same
          value. `.fig { width:100% }` would otherwise stretch the 460-wide
          viewBox to the full column and upscale the whole semicircle (H, R,
          and its bottom-anchored centre all scale with W) right off the
          fold. The inner box pins width = the clamped W so viewBox px = CSS
          px (1:1), centred in the row. */}
      <Fig fallback={300}>
        {(w) => {
          const cw = Math.min(w, 460);
          return (
            <div style={{ width: cw, maxWidth: '100%', margin: '0 auto' }}>
              <ChordZ z={z} W={cw} />
            </div>
          );
        }}
      </Fig>
      <p className="zk-prop-note" style={{ margin: '6px 2px 0', lineHeight: 1.5 }}>
        hover an agent to <b>project</b> its conversations (click to pin · click
        away to clear) · ribbon width grows with √count and caps, so heavy traffic
        can never flood the figure
      </p>

      <h3>drift seismograph + judge heartbeat — one instrument, one time axis</h3>
      <div className="track-stack">
        <Fig>{(w) => <SeismographZ z={z} W={w} axis={false} />}</Fig>
        <div className="track-sub">
          interventions, time-aligned beneath the drift above ↓
        </div>
        <Fig>{(w) => <LadderZ z={z} W={w} />}</Fig>
      </div>
    </>
  );
}
