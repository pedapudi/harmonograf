import type { SpanKind, SpanStatus } from './types';

// Resolves kind × status → final fill + stroke + treatment at render time.
// Hues come from the CSS custom properties set by src/theme/themes.ts so a
// theme switch re-colors the canvas on the next frame without re-running any
// React code.

export interface ResolvedStyle {
  fill: string;
  stroke: string | null;
  // Opacity applied on top of the fill alpha channel (for PENDING, PLANNED,
  // CANCELLED, replaced). 1 = no change.
  opacity: number;
  // If true, renderer draws a diagonal hatch overlay.
  hatched: boolean;
  // If true, renderer draws a dashed outline (PLANNED).
  dashed: boolean;
  // 0 = no animation; 1 = breathing (2s loop); 2 = pulse (1s loop).
  animation: 0 | 1 | 2;
  // True for FAILED: renderer paints a red warning icon in the top-right.
  errorIcon: boolean;
}

const KIND_TO_VAR: Record<SpanKind, string> = {
  INVOCATION: '--hg-kind-invocation',
  LLM_CALL: '--hg-kind-llm-call',
  TOOL_CALL: '--hg-kind-tool-call',
  USER_MESSAGE: '--hg-kind-user-message',
  AGENT_MESSAGE: '--hg-kind-agent-message',
  TRANSFER: '--hg-kind-transfer',
  WAIT_FOR_HUMAN: '--hg-kind-wait-for-human',
  PLANNED: '--hg-kind-custom',
  CUSTOM: '--hg-kind-custom',
};

let cachedRoot: CSSStyleDeclaration | null = null;
let cachedTheme = '';

function rootStyles(): CSSStyleDeclaration {
  const theme = document.documentElement.dataset.theme ?? '';
  if (!cachedRoot || theme !== cachedTheme) {
    cachedRoot = getComputedStyle(document.documentElement);
    cachedTheme = theme;
  }
  return cachedRoot;
}

export function refreshThemeCache(): void {
  cachedRoot = null;
}

export function cssVar(name: string): string {
  return rootStyles().getPropertyValue(name).trim() || '#888888';
}

export function kindBaseColor(kind: SpanKind): string {
  return cssVar(KIND_TO_VAR[kind]);
}

export function resolveStyle(kind: SpanKind, status: SpanStatus, replaced: boolean): ResolvedStyle {
  const base: ResolvedStyle = {
    fill: kindBaseColor(kind),
    stroke: null,
    opacity: 1,
    hatched: false,
    dashed: false,
    animation: 0,
    errorIcon: false,
  };

  if (kind === 'PLANNED') {
    base.opacity = 0.3;
    base.dashed = true;
  }
  if (kind === 'INVOCATION') {
    // Container recedes behind children.
    base.opacity = 0.6;
  }

  switch (status) {
    case 'PENDING':
      base.opacity *= 0.4;
      break;
    case 'RUNNING':
      base.animation = 1;
      break;
    case 'COMPLETED':
      break;
    case 'FAILED':
      base.fill = cssVar('--md-sys-color-error');
      base.errorIcon = true;
      break;
    case 'CANCELLED':
      base.opacity *= 0.3;
      base.hatched = true;
      break;
    case 'AWAITING_HUMAN':
      base.fill = cssVar('--md-sys-color-error-container');
      base.stroke = cssVar('--md-sys-color-error');
      base.animation = 2;
      break;
  }

  if (replaced) base.opacity *= 0.3;
  return base;
}

// Buckets spans by a string key for batched fillRect passes. The key combines
// fill, hatch, and dash so we never change style mid-batch.
export function bucketKey(s: ResolvedStyle): string {
  return `${s.fill}|${s.hatched ? 'h' : ''}|${s.dashed ? 'd' : ''}|${s.opacity.toFixed(2)}`;
}
