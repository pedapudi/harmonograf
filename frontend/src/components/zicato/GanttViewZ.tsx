// GanttViewZ.tsx — the hero rail view: the gantt stands alone, full size, with
// the judge heartbeat underneath. (compose.html mainHybrid gantt branch 714-717.)

import { useUiStore } from '../../state/uiStore';
import type { ZSession } from './adapter';
import { Fig } from './Fig';
import { GanttZ } from './GanttZ';
import { JudgeHeartbeatZ } from './SeismographZ';

export interface GanttViewZProps {
  z: ZSession;
}

export function GanttViewZ({ z }: GanttViewZProps) {
  const selectedSpanId = useUiStore((s) => s.selectedSpanId);
  const selectSpan = useUiStore((s) => s.selectSpan);
  return (
    <>
      <h3>execution — gantt</h3>
      <div className="gantt-click">
        <Fig>
          {(w) => (
            <GanttZ
              z={z}
              W={w}
              selectedSpanId={selectedSpanId}
              onSpanSelect={(id) => selectSpan(id)}
            />
          )}
        </Fig>
      </div>
      <h3>judge heartbeat</h3>
      <Fig>{(w) => <JudgeHeartbeatZ z={z} W={w} />}</Fig>
    </>
  );
}
