// steerContext.ts — routes a Gantt steering-arrow click up to the console's
// floating drawer WITHOUT threading a prop through the do-not-edit
// GanttViewZ/Fig layers. ZicatoConsole provides the handler via
// <SteerSelectContext.Provider>; GanttZ consumes it with useSteerSelect() as
// the default `onSteerSelect`. A no-op default keeps GanttZ safe when rendered
// standalone (tests, the minimap, future call sites outside the console).
//
// Lives in its own non-component module so FloatingDrawerZ.tsx /
// ZicatoConsole.tsx stay Fast-Refresh-clean (a file that exports a React
// component may not also export a hook).

import { createContext, useContext } from 'react';
import type { ZSteer } from './adapter';

export type SteerSelectHandler = (steer: ZSteer) => void;

export const SteerSelectContext = createContext<SteerSelectHandler>(() => {});

/** GanttZ's default steering-click handler — the console's drawer opener. */
export function useSteerSelect(): SteerSelectHandler {
  return useContext(SteerSelectContext);
}
