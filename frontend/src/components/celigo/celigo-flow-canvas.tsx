"use client";

/**
 * Task 15 — the real layered canvas (mockup screen 3's `.canvas`): bubbles,
 * router nodes, branch lanes, edges, fit/zoom, selection. Replaces the Task
 * 14 stub of the same name — the prop contract (`detail`, `selectedStepId`,
 * `onSelectStep`, `paused`) is unchanged, so `celigo-flow-page.tsx`'s wiring
 * never has to change for this to land.
 *
 * Geometry comes entirely from Task 13's `computeLayout` (pure, deterministic
 * — a refetch of the same synced flow never reshuffles the canvas); this
 * file owns only DISPLAY: the bubble/router-node renderers (Task 15's own
 * `step-bubble.tsx` / `router-node.tsx`), the SVG edges, the branch-lane
 * captions, and the fit-to-width/100% zoom toggle.
 *
 * `paused` only affects how bubbles draw (dimmed to 60%, mockup screen 3) —
 * the paused BANNER above the canvas is `celigo-flow-page.tsx`'s own.
 */

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CeligoFlowDetail } from "@/hooks/use-celigo-flows";
import type { InspectorTab } from "./celigo-step-inspector";
import { computeLayout, type LayoutEdge, type LayoutRouter } from "./layout";
import { StepBubble } from "./step-bubble";
import { RouterNode } from "./router-node";
import { cn } from "@/lib/utils";

const ARROW_MARKER_ID = "celigo-flow-arrowhead";
const FIT_FLOOR = 0.6;

function clamp(n: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, n));
}

/** A straight line for a same-rank spine/branch-row edge, a horizontal-first
 * bezier for a router's curved fan-out into a lane — same curve shape the
 * approved mockup draws (`C {midX},{y1} {midX},{y2} {x2},{y2}`). */
function edgePath(edge: Pick<LayoutEdge, "x1" | "y1" | "x2" | "y2" | "curved">): string {
  if (!edge.curved) return `M${edge.x1},${edge.y1} L${edge.x2},${edge.y2}`;
  const midX = (edge.x1 + edge.x2) / 2;
  return `M${edge.x1},${edge.y1} C${midX},${edge.y1} ${midX},${edge.y2} ${edge.x2},${edge.y2}`;
}

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
  const layout = useMemo(() => computeLayout(detail), [detail]);
  const stepsById = useMemo(() => new Map(detail.steps.map((s) => [s.id, s])), [detail.steps]);
  // Resolved against `layout.routers`, NOT `detail.routers`: the layout also
  // draws a node for any router the flow's steps point at that `detail.routers`
  // never declared. Looking those up in the declared list missed, `RouterNode`
  // returned null, and the rank the layout had already reserved rendered as
  // blank space with an edge arriving in it (finding I4).
  const routersById = useMemo(() => {
    const m = new Map<string, LayoutRouter>();
    for (const r of layout.routers) m.set(r.id, r);
    return m;
  }, [layout.routers]);

  // Router display numbers ("Router 1", "Router 2", …) follow the order
  // `computeLayout` actually placed the router NODES in — the chain's
  // declared walk order, never a re-sort — so a pass-through router's
  // "chains to router N" names the same number this canvas prints on that
  // target node.
  const routerIndexById = useMemo(() => {
    const m = new Map<string, number>();
    let i = 0;
    for (const node of layout.nodes) {
      if (node.type === "router" && node.routerId && !m.has(node.routerId)) m.set(node.routerId, i++);
    }
    return m;
  }, [layout.nodes]);

  const wrapRef = useRef<HTMLDivElement>(null);
  const [mode, setMode] = useState<"fit" | "100">("fit");
  const [fitScale, setFitScale] = useState(1);

  useLayoutEffect(() => {
    function recompute() {
      const avail = wrapRef.current?.clientWidth ?? 0;
      setFitScale(layout.width > 0 ? clamp(avail / layout.width, FIT_FLOOR, 1) : 1);
    }
    recompute();
    if (typeof ResizeObserver === "undefined" || !wrapRef.current) return;
    const ro = new ResizeObserver(recompute);
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, [layout.width]);

  const scale = mode === "100" ? 1 : fitScale;
  // At the floor the diagram does NOT fit the viewport — the wrap scrolls —
  // so calling it "fit" tells a reader the whole flow is on screen when part
  // of it is off to the right, and they stop scrolling. The floor itself
  // stays: below 60% the bubbles are unreadable, and a scroll is the better
  // trade (ruling R19b). Only the label changes.
  const fitClamped = mode === "fit" && fitScale <= FIT_FLOOR;
  const zoomLabel =
    mode === "100" ? "100%" : `${fitClamped ? "min" : "fit"} · ${Math.round(fitScale * 100)}%`;

  return (
    <div data-testid="celigo-flow-canvas" className="flex h-full flex-col">
      <div
        ref={wrapRef}
        data-testid="celigo-canvas-wrap"
        className="relative flex-1 overflow-auto bg-muted/30"
        style={{ backgroundImage: "radial-gradient(hsl(var(--border)) 1px, transparent 1px)", backgroundSize: "18px 18px" }}
      >
        <div style={{ width: layout.width * scale, height: layout.height * scale, overflow: "hidden" }}>
          <div
            style={{ width: layout.width, height: layout.height, transform: `scale(${scale})`, transformOrigin: "0 0", position: "relative" }}
          >
            <svg
              width={layout.width}
              height={layout.height}
              viewBox={`0 0 ${layout.width} ${layout.height}`}
              aria-hidden="true"
              className="pointer-events-none absolute inset-0"
            >
              <defs>
                <marker id={ARROW_MARKER_ID} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
                  <path d="M0,0 L8,4 L0,8 z" className="fill-muted-foreground" />
                </marker>
              </defs>
              {layout.edges.map((edge) => (
                <path
                  key={edge.id}
                  d={edgePath(edge)}
                  markerEnd={`url(#${ARROW_MARKER_ID})`}
                  strokeDasharray={edge.dashed ? "4 4" : undefined}
                  className={cn("fill-none stroke-[1.5px]", edge.dashed ? "stroke-amber-500" : "stroke-border")}
                />
              ))}
            </svg>

            {layout.lanes.map((lane) => (
              <div
                key={`${lane.routerId}:${lane.branchId}`}
                data-testid={`lane-label-${lane.routerId}-${lane.branchId}`}
                style={{ position: "absolute", left: lane.x, top: lane.y }}
                className="whitespace-nowrap text-[10.5px] text-muted-foreground"
              >
                {/* A lane standing in for several id-less branches (ruling
                    R19a) has no branch of its own to name: printing "Branch 1
                    · Unnamed · 0 rules" would read as one specific branch that
                    happens to lack a name, when the truth is that nothing
                    attributes these steps to any of the N. */}
                {lane.mergedBranchCount ? (
                  <b className="font-medium text-foreground">
                    {`${lane.mergedBranchCount} branches · steps not attributable (branch ids missing)`}
                  </b>
                ) : (
                  <>
                    <b className="font-medium text-foreground">{`Branch ${lane.order + 1} · ${lane.name ?? "Unnamed"}`}</b>
                    {` · ${lane.ruleCount} rule${lane.ruleCount === 1 ? "" : "s"}`}
                  </>
                )}
              </div>
            ))}

            {layout.nodes.map((node) => {
              if (node.type === "step" && node.stepId) {
                const step = stepsById.get(node.stepId);
                if (!step) return null;
                return (
                  <StepBubble key={node.id} step={step} node={node} selected={selectedStepId === step.id} paused={paused} onSelect={onSelectStep} />
                );
              }
              if (node.type === "router" && node.routerId) {
                const router = routersById.get(node.routerId);
                if (!router) return null;
                return (
                  <RouterNode
                    key={node.id}
                    router={router}
                    node={node}
                    index={routerIndexById.get(node.routerId) ?? 0}
                    routerIndexById={routerIndexById}
                  />
                );
              }
              return (
                <div
                  key={node.id}
                  data-testid={`placeholder-${node.id}`}
                  style={{ position: "absolute", left: node.x, top: node.y, width: node.w, height: node.h }}
                  className="flex items-center justify-center rounded-xl border border-dashed border-border text-[11px] text-muted-foreground"
                >
                  no steps declared
                </div>
              );
            })}
          </div>
        </div>

        {layout.warnings.length > 0 && (
          <div
            data-testid="canvas-warnings"
            className="absolute left-2 top-2 z-10 rounded bg-amber-500/10 px-2 py-1 text-[10.5px] text-amber-700 dark:text-amber-400"
          >
            {layout.warnings.join(" · ")}
          </div>
        )}

        <div className="absolute bottom-2 right-2 z-10 flex items-center gap-1">
          <span className="text-[10.5px] text-muted-foreground">{zoomLabel}</span>
          <button
            type="button"
            aria-label="Zoom to 100%"
            onClick={() => setMode("100")}
            className="flex h-[26px] w-[26px] items-center justify-center rounded-md border bg-card text-[12px] text-muted-foreground"
          >
            +
          </button>
          <button
            type="button"
            aria-label="Fit to width"
            onClick={() => setMode("fit")}
            className="flex h-[26px] w-[26px] items-center justify-center rounded-md border bg-card text-[12px] text-muted-foreground"
          >
            ⤢
          </button>
        </div>
      </div>

      <div data-testid="canvas-legend" className="flex flex-wrap items-center gap-3 border-t bg-card px-4 py-2 text-[11px] text-muted-foreground">
        <span>Chip states:</span>
        <span className="rounded bg-blue-500/10 px-1.5 py-px text-blue-700 dark:text-blue-400">configured · named</span>
        <span className="rounded border border-border px-1.5 py-px">looked, none</span>
        <span className="rounded border border-dashed border-border px-1.5 py-px">cannot say · not synced</span>
        <span className="ml-auto">
          Canvas shown at fit-to-width; <b className="font-medium text-foreground">+</b> is 100%.
        </span>
      </div>
    </div>
  );
}
