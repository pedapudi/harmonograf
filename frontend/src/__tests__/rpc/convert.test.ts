// Unit coverage for the proto → UI conversion seam. Focuses on the
// timestamp-normalisation behaviour that ``convertAnnotation`` needs to
// get right for the intervention deriver to plot rows in the same
// session-relative coordinate space as spans and plans (harmonograf#86).

import { describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { TimestampSchema } from '@bufbuild/protobuf/wkt';
import { convertAnnotation } from '../../rpc/convert';
import {
  AnnotationSchema,
  AnnotationKind,
  AnnotationTargetSchema,
} from '../../pb/harmonograf/v1/types_pb';

function ts(seconds: number, nanos: number = 0) {
  return create(TimestampSchema, {
    seconds: BigInt(Math.trunc(seconds)),
    nanos,
  });
}

describe('convertAnnotation', () => {
  it('normalises createdAtMs / deliveredAtMs to session-relative ms', () => {
    // Session started at wall-clock 1_000_000 (ms since epoch). An
    // annotation created 322s into the session and delivered 500ms
    // later should surface as 322_000 / 322_500 on the UI side —
    // matching the coordinate space of spans and plans so the
    // Interventions list can plot the row alongside the others.
    const origin = { startMs: 1_000_000 };
    const createdAtAbsSec = 1_322;
    const deliveredAtAbsSec = 1_322;
    const pb = create(AnnotationSchema, {
      id: 'ann_1',
      sessionId: 'sess',
      target: create(AnnotationTargetSchema, {
        target: {
          case: 'agentTime',
          value: {
            agentId: 'a',
            at: ts(1_322),
          },
        },
      }),
      author: 'alice',
      kind: AnnotationKind.STEERING,
      body: 'pivot',
      createdAt: ts(createdAtAbsSec, 0),
      deliveredAt: ts(deliveredAtAbsSec, 500_000_000), // +500ms
    });
    const ui = convertAnnotation(pb, origin);
    expect(ui.createdAtMs).toBe(322_000);
    expect(ui.deliveredAtMs).toBe(322_500);
    // The agent-time atMs is already session-relative from the pre-#86
    // code path; spot-check it so a regression on that field also trips.
    expect(ui.atMs).toBe(322_000);
  });

  it('leaves createdAtMs at 0 when the pb field is unset', () => {
    const origin = { startMs: 1_000_000 };
    const pb = create(AnnotationSchema, {
      id: 'ann_2',
      sessionId: 'sess',
      author: 'bob',
      kind: AnnotationKind.COMMENT,
      body: 'note',
      // no createdAt / deliveredAt
    });
    const ui = convertAnnotation(pb, origin);
    expect(ui.createdAtMs).toBe(0);
    expect(ui.deliveredAtMs).toBeNull();
  });
});
