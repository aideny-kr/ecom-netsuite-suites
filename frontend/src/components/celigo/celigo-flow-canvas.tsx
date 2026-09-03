"use client";

/**
 * Task 15 replaces this with the real layered canvas (mockup screen 3's
 * `.canvas`: bubbles, router nodes, branch lanes, the fit/zoom controls).
 * Task 14 locks the FINAL prop contract now — `celigo-flow-page.tsx`'s
 * wiring (and its tests) never need to change when Task 15 lands, only this
 * file's body does.
 *
 * The page itself (not this component) handles the two states that don't
 * need a canvas at all: an empty flow ("No steps recorded for this flow in
 * the last sync.") never mounts this component, and the paused banner is
 * the page's own — `paused` here only needs to affect how bubbles draw
 * once Task 15 builds them (the mockup dims them to 60%).
 */

import type { CeligoFlowDetail } from "@/hooks/use-celigo-flows";
import type { InspectorTab } from "./celigo-step-inspector";

export function CeligoFlowCanvas({
  detail,
  selectedStepId,
  onSelectStep,
  paused,
}: {
  detail: CeligoFlowDetail;
  selectedStepId: string | null;
  onSelectStep: (stepId: string | null, tab?: InspectorTab) => void;
  paused: boolean;
}): JSX.Element {
  void selectedStepId;
  void onSelectStep;
  return (
    <div
      data-testid="celigo-flow-canvas-stub"
      data-paused={paused ? "true" : undefined}
      className="flex h-full items-center justify-center text-[12px] text-muted-foreground"
    >
      {`Canvas — ${detail.steps.length} step${detail.steps.length === 1 ? "" : "s"} (Task 15)`}
    </div>
  );
}
