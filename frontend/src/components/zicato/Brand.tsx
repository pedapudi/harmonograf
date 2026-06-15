// Brand.tsx — the harmonograf brand lockup for the zicato topbar (+ reuse in the
// ⌘K picker). JSX port of compose.html 149-157. Pure — no session data.
//
//   <BrandMark height={20} />   the α-mark (one period of a 2:3 Lissajous,
//                               mirrored about the vertical axis → lowercase α)
//   <Wordmark />                plain mono "harmonograf"

import { hgAlphaPath } from './svgUtils';

export function BrandMark({ height = 24 }: { height?: number }) {
  return (
    <svg
      className="hg-brand-mark"
      style={{ height: `${height}px` }}
      viewBox="0 0 48 48"
      role="img"
      aria-label="harmonograf"
      focusable="false"
    >
      <path
        d={hgAlphaPath(24, 24, 19, 15)}
        fill="none"
        stroke="currentColor"
        strokeWidth={2.6}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* the one dot rides the lower-right tail tip — the pen at rest. */}
      <circle cx={43} cy={39} r={3.2} fill="var(--hgraf-brand, #5EA3EC)" />
    </svg>
  );
}

export function Wordmark() {
  const T = 'harmonograf';
  const X0 = 1;
  const ADV = 9.6;
  const BASE = 15;
  const W = X0 * 2 + T.length * ADV;
  return (
    <svg
      className="hg-brand-name"
      viewBox={`0 0 ${W} 18`}
      role="img"
      aria-label="harmonograf"
      focusable="false"
    >
      <text
        x={X0}
        y={BASE}
        fontFamily="var(--brand-mono)"
        fontSize={15}
        fontWeight={700}
        textLength={T.length * ADV}
        lengthAdjust="spacing"
        fill="currentColor"
        xmlSpace="preserve"
      >
        {T}
      </text>
    </svg>
  );
}
