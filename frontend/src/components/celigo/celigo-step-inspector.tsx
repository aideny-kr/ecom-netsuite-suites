"use client";

/**
 * Task 16 replaces this with the real tabbed inspector (mockup screen 3's
 * `.insp`: Facts / Filter / Mapping / Scripts / Errors, per-step chips and
 * script sites, the script-drawer trigger). Task 14 locks the FINAL prop
 * contract now — `celigo-flow-page.tsx`'s wiring (and its tests) never need
 * to change when Task 16 lands, only this file's body does.
 *
 * `InspectorTab` lives here (not in `celigo-flow-page.tsx`) because it is
 * this component's own vocabulary — `celigo-flow-canvas.tsx`'s
 * `onSelectStep` takes one too (a hook-chip click jumps straight to the
 * Scripts tab), so it imports the type from here rather than the page
 * re-exporting it.
 */

import type { CeligoFlowDetail, CeligoFlowStep } from "@/hooks/use-celigo-flows";

export type InspectorTab = "facts" | "filter" | "mapping" | "scripts" | "errors";

export function CeligoStepInspector({
  detail,
  step,
  tab,
  onTabChange,
  lastSyncedAt,
  onOpenScript,
}: {
  detail: CeligoFlowDetail;
  step: CeligoFlowStep | null;
  tab: InspectorTab;
  onTabChange: (tab: InspectorTab) => void;
  lastSyncedAt: string | null;
  onOpenScript: (scriptId: string) => void;
}): JSX.Element {
  void onTabChange;
  void lastSyncedAt;
  void onOpenScript;
  return (
    <div
      data-testid="celigo-step-inspector-stub"
      data-tab={tab}
      className="flex h-full flex-col p-3 text-[12px] text-muted-foreground"
    >
      {step
        ? `Inspector — ${step.kind} step (Task 16)`
        : `Inspector — no step selected (Task 16) · ${detail.name}`}
    </div>
  );
}
