// InstrumentsViewZ.tsx — everything that is NOT the gantt, coalesced into one
// mission-control scroll: THE PLAN LEADS (reel → DAG), then conversation +
// topology side-by-side, then ONE coordinated time-track stack (drift+judge
// seismograph over the time-aligned intervention ladder).
// (compose.html mainHybrid instruments branch 718-735.)

import type { ZSession } from './adapter';
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

      <PlanZ z={z} />

      <div className="zk-panes-2">
        <div className="gantt-click">
          <h3>sequence — who said what to whom, when</h3>
          <SequenceZ z={z} W={520} H={380} />
        </div>
        <div>
          <h3>topology — who initiates, who receives</h3>
          <ChordZ z={z} W={300} />
          <p
            className="zk-prop-note"
            style={{ margin: '6px 2px 0', lineHeight: 1.5 }}
          >
            hover an agent to <b>project</b> its conversations (click to pin · click
            away to clear) · ribbon width grows with √count and caps, so heavy
            traffic can never flood the figure
          </p>
        </div>
      </div>

      <h3>drift seismograph + judge heartbeat — one instrument, one time axis</h3>
      <div className="track-stack">
        <SeismographZ z={z} W={940} axis={false} />
        <div className="track-sub">
          interventions, time-aligned beneath the drift above ↓
        </div>
        <LadderZ z={z} W={940} />
      </div>
    </>
  );
}
