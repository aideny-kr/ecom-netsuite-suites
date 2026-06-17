"use client";

import { useMemo } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  Position,
  type Node,
  type Edge,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationNodeDatum,
} from "d3-force";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  useUpdateConceptReview,
  type MemoryConcept,
  type MemoryGraph,
  type MemoryReviewState,
} from "@/hooks/use-memory-graph";

// Colour a node by its review_state. confirmed=emerald (active in chat),
// rejected=destructive, pending=amber, merged=muted.
const STATE_STYLES: Record<MemoryReviewState, string> = {
  confirmed: "border-emerald-500 bg-emerald-50 dark:bg-emerald-950/40",
  rejected: "border-destructive bg-destructive/10",
  pending: "border-amber-500 bg-amber-50 dark:bg-amber-950/40",
  merged: "border-muted-foreground/40 bg-muted",
};

type ConceptNodeData = { concept: MemoryConcept };

function ConceptNode({ data }: NodeProps<ConceptNodeData>) {
  const { concept } = data;
  const updateReview = useUpdateConceptReview();

  return (
    <div
      data-testid={`concept-node-${concept.id}`}
      className={cn(
        "min-w-[200px] max-w-[260px] rounded-xl border-2 p-3 shadow-soft",
        STATE_STYLES[concept.review_state],
      )}
    >
      <Handle type="target" position={Position.Top} className="!bg-muted-foreground" />
      <div className="flex items-start justify-between gap-2">
        <span className="text-[13px] font-semibold text-foreground">{concept.name}</span>
        {concept.concept_type && (
          <Badge variant="secondary" className="shrink-0 text-[10px]">
            {concept.concept_type}
          </Badge>
        )}
      </div>
      {concept.summary && (
        <p className="mt-1.5 line-clamp-3 text-[12px] text-muted-foreground">
          {concept.summary}
        </p>
      )}
      {concept.review_state === "pending" && (
        <Button
          size="sm"
          data-testid={`confirm-concept-${concept.id}`}
          className="mt-2.5 h-7 w-full bg-emerald-600 text-white hover:bg-emerald-700"
          disabled={updateReview.isPending}
          onClick={() =>
            updateReview.mutate({ id: concept.id, review_state: "confirmed" })
          }
        >
          Confirm
        </Button>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-muted-foreground" />
    </div>
  );
}

const nodeTypes = { concept: ConceptNode };

type SimNode = SimulationNodeDatum & { id: string };
type SimLink = { source: string; target: string };

// Force-directed (Obsidian-style) layout: nodes repel, edges pull related concepts
// together, so the graph settles into an organic cluster instead of a rigid grid.
// Seeded on a circle (no Math.random) + run to completion synchronously, so the
// layout is deterministic and stable across re-renders (confirming a concept won't
// reshuffle the whole graph). reactflow renders the settled positions + fitView.
function layoutPositions(
  concepts: MemoryConcept[],
  edges: MemoryGraph["edges"],
): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  const n = concepts.length;
  if (n === 0) return positions;

  const ring = 70 * Math.sqrt(n);
  const simNodes: SimNode[] = concepts.map((c, i) => {
    const a = (i / n) * 2 * Math.PI;
    return { id: c.id, x: Math.cos(a) * ring, y: Math.sin(a) * ring };
  });

  const present = new Set(simNodes.map((s) => s.id));
  const simLinks: SimLink[] = edges
    .filter((e) => present.has(e.source_concept_id) && present.has(e.target_concept_id))
    .map((e) => ({ source: e.source_concept_id, target: e.target_concept_id }));

  forceSimulation(simNodes)
    .force("charge", forceManyBody().strength(-1400))
    .force(
      "link",
      forceLink<SimNode, SimLink>(simLinks)
        .id((d) => d.id)
        .distance(230)
        .strength(0.35),
    )
    .force("center", forceCenter(0, 0))
    .force("collide", forceCollide(150))
    .stop()
    .tick(300);

  for (const s of simNodes) positions.set(s.id, { x: s.x ?? 0, y: s.y ?? 0 });
  return positions;
}

function toFlow(graph: MemoryGraph): { nodes: Node<ConceptNodeData>[]; edges: Edge[] } {
  const presentIds = new Set(graph.concepts.map((c) => c.id));
  const positions = layoutPositions(graph.concepts, graph.edges);

  const nodes: Node<ConceptNodeData>[] = graph.concepts.map((concept) => ({
    id: concept.id,
    type: "concept",
    position: positions.get(concept.id) ?? { x: 0, y: 0 },
    data: { concept },
  }));

  const edges: Edge[] = graph.edges
    // Drop dangling edges so reactflow does not warn about missing endpoints.
    .filter(
      (e) => presentIds.has(e.source_concept_id) && presentIds.has(e.target_concept_id),
    )
    .map((e) => ({
      id: e.id,
      source: e.source_concept_id,
      target: e.target_concept_id,
      label: e.relation,
      animated: e.review_state === "pending",
    }));

  return { nodes, edges };
}

export function MemoryGraphCanvas({ graph }: { graph: MemoryGraph }) {
  const { nodes, edges } = useMemo(() => toFlow(graph), [graph]);

  return (
    <div data-testid="memory-graph-canvas" className="h-[640px] w-full rounded-xl border bg-card">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls />
      </ReactFlow>
    </div>
  );
}
