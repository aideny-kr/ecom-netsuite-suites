import type { CeligoFlowDetail, CeligoFlowStep, CeligoRouter, CeligoRouterBranch } from "@/hooks/use-celigo-flows";

// Task 13 -- a pure, deterministic left-to-right layered layout over a flow's
// steps + routers (no React, no DOM: this is geometry only). "Deterministic"
// is the whole point -- a refetch of the same synced flow must never reshuffle
// the canvas, so every position below is a function of (sequence, declared
// router/branch order) alone, never of iteration order over a Map/Set or of
// anything the caller didn't explicitly declare.
//
// This module computes ONLY structure and geometry -- ids, coordinates, edge
// routing, warnings. It deliberately carries no display text (fallback step
// titles, "N branches · open in Celigo", router summary lines): those read
// straight off the same `CeligoFlowDetail` the caller already has, in the
// canvas renderer (Task 15), so a copy-rule change never has to touch this
// file. That is also why `captions` below is always `[]` -- nothing in this
// module's algorithm produces caption text; the field exists on `Layout`
// for a renderer-owned use the plan never assigns to this task.

export const BUBBLE_W = 212,
  BUBBLE_H = 162,
  ROUTER_W = 110,
  ROUTER_H = 56,
  GAP_X = 22,
  LANE_PITCH = 252,
  MARGIN = 20,
  LANE_LABEL_H = 24;

export type LayoutNode = {
  id: string;
  type: "step" | "router" | "placeholder";
  x: number;
  y: number;
  w: number;
  h: number;
  stepId?: string;
  routerId?: string;
  branchId?: string | null;
  lane?: number;
};

export type LayoutEdge = {
  id: string;
  from: string;
  to: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  curved: boolean;
  dashed: boolean;
  label?: string;
};

export type LaneLabel = {
  routerId: string;
  /** `branchKey`, NOT the raw `branch.id` — a branch that declares no id gets
   * a positional stand-in so the caller can use this as a React key / DOM id
   * without two unnamed lanes colliding. */
  branchId: string;
  name: string | null;
  ruleCount: number;
  order: number;
  x: number;
  y: number;
};

export type Layout = {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  lanes: LaneLabel[];
  /** Every router this layout drew a node for, keyed by the `routerId` those
   * nodes carry — the flow's declared routers PLUS the synthetic entries this
   * module invents for a `router_id` steps reference but `detail.routers`
   * never declared. A renderer must resolve router nodes against THIS list,
   * not `detail.routers`: a lookup that misses on a synthetic router leaves a
   * reserved rank blank with an edge pointing into it (finding I4). */
  routers: LayoutRouter[];
  captions: { x: number; y: number; text: string }[];
  width: number;
  height: number;
  warnings: string[];
};

const WARN_ROUTER_ORDER_UNVERIFIED = "router order unverified";
const WARN_NESTED_ROUTER_INLINE = "nested router chain drawn inline";

/** A router entry in the working chain: every declared router from
 * `detail.routers`, verbatim, plus a synthetic one per undeclared router_id
 * actually referenced by a step -- same shape either way so the rest of the
 * algorithm never has to branch on "declared vs synthetic" again once this
 * list exists.
 *
 * `declared` is the one thing a RENDERER still has to tell apart: a synthetic
 * router carries no name, no routing mode and no true branch order, so
 * printing the usual "first matching branch · by input filters · N" over it
 * would state facts the sync never supplied. It is exported on `Layout.routers`
 * for exactly that. */
export type LayoutRouter = CeligoRouter & { id: string; declared: boolean };

type RouterEntry = LayoutRouter;

function bySequence(a: CeligoFlowStep, b: CeligoFlowStep): number {
  return a.sequence - b.sequence;
}

/** The stable key for one branch inside every id this module mints — node ids,
 * edge ids, lane keys. A declared branch has Celigo's own generated `id`,
 * unique account-wide; a branch that declares none falls back to its position
 * INSIDE ITS OWN ROUTER, so two unnamed branches — of the same router or of
 * two different ones — can never collapse onto one key.
 *
 * They used to (final-review finding I2): every id was built from the raw
 * `branch.id`, so a router with two unnamed branches produced two lanes keyed
 * `r:null`, and — via `stepsForBranch` below — the same step node twice. React
 * renders duplicate keys as one element, so a whole lane silently vanished. */
function branchKey(routerId: string, branch: CeligoRouterBranch, index: number): string {
  return branch.id ?? `${routerId}#${index}`;
}

/** One branch's steps, by sequence.
 *
 * A branch that declares no id would match the router's `branch_id IS NULL`
 * steps — and so would every OTHER unnamed branch of that router, which is how
 * one step ended up drawn in every unnamed lane at once. Only the FIRST
 * unnamed branch claims them now: the sync gives us nothing that says which
 * unnamed lane an unattributed step belongs to, so putting it in all of them
 * asserted something Celigo never told us. Later unnamed branches fall through
 * to the ordinary "no steps" placeholder. */
function stepsForBranch(
  routerSteps: CeligoFlowStep[],
  router: RouterEntry,
  branch: CeligoRouterBranch,
  index: number,
): CeligoFlowStep[] {
  if (branch.id === null && index !== router.branches.findIndex((b) => b.id === null)) return [];
  return routerSteps.filter((s) => s.router_id === router.id && s.branch_id === branch.id).sort(bySequence);
}

/** Declared routers (as given) plus a synthetic entry -- `name: null`,
 * branches built from the distinct `branch_id`s actually used, sorted
 * lexically since nothing about an undeclared router's true order survived
 * the sync -- for any router_id a step references that `detail.routers`
 * never declared. Pushes the "router order unverified" warning once per
 * synthetic entry: an undeclared router's position in the drawing is a
 * guess, and every caller of this list needs to know that up front. */
function buildRouterList(routers: CeligoRouter[], routerSteps: CeligoFlowStep[], warnings: string[]): RouterEntry[] {
  const declared: RouterEntry[] = routers
    .filter((r): r is CeligoRouter & { id: string } => typeof r.id === "string")
    .map((r) => ({ ...r, declared: true }));
  const declaredIds = new Set(declared.map((r) => r.id));

  const undeclaredIds: string[] = [];
  const seenUndeclared = new Set<string>();
  for (const s of routerSteps) {
    if (s.router_id && !declaredIds.has(s.router_id) && !seenUndeclared.has(s.router_id)) {
      seenUndeclared.add(s.router_id);
      undeclaredIds.push(s.router_id);
    }
  }

  const synthetic: RouterEntry[] = undeclaredIds.map((routerId) => {
    warnings.push(WARN_ROUTER_ORDER_UNVERIFIED);
    const ownSteps = routerSteps.filter((s) => s.router_id === routerId);
    const branchIds: (string | null)[] = Array.from(
      new Set(ownSteps.filter((s) => s.branch_id !== null).map((s) => s.branch_id as string)),
    ).sort();
    // Steps that name this router but NO branch used to belong to no lane at
    // all: the branch list was built only from the non-null `branch_id`s, so
    // those steps vanished -- no node, no placeholder, no warning -- while
    // the router they pointed at was drawn as if it were complete. They get
    // a leading id-less branch instead (`stepsForBranch`'s first-null-branch
    // rule then claims exactly them), and the flow gets told what the sync
    // could not say: which lane they really belong to is unknown.
    const unbranchedCount = ownSteps.filter((s) => s.branch_id === null).length;
    if (unbranchedCount > 0) {
      branchIds.unshift(null);
      warnings.push(`undeclared router: ${unbranchedCount} step(s) without a branch`);
    }
    const branches: CeligoRouterBranch[] = branchIds.map((id, order) => ({
      id,
      name: null,
      rule_count: 0,
      next_router_id: null,
      order,
      declared_step_count: 0,
    }));
    return {
      id: routerId,
      name: null,
      route_records_to: null,
      route_records_using: null,
      has_script_slot: false,
      branches,
      declared: false,
    };
  });

  return [...declared, ...synthetic];
}

/** One item along the spine's router segment: either the router node itself,
 * one inline step of a pass-through router's single branch, or a placeholder
 * standing in for that branch when it declares no steps at all. */
type SpineRouterItem =
  | { kind: "router"; router: RouterEntry }
  | { kind: "step"; step: CeligoFlowStep; routerId: string; branchId: string | null }
  | { kind: "placeholder"; routerId: string; branchId: string | null; branchKey: string };

/** Walks the router chain from its declared entry point (the first router in
 * `combined`) while every router visited is *pass-through* -- exactly one
 * branch whose `next_router_id` names another router in `combined` -- laying
 * that branch's steps inline on the spine between the two router nodes. The
 * walk stops at the first router that is NOT pass-through (more than one
 * branch, or a single branch with no `next_router_id`): that router becomes
 * the fan-out router whose branches become lanes. Any router `combined`
 * lists but this walk never reaches is returned separately as `remaining`
 * (declared but orphaned from the chain, or synthetic-and-still-unconsumed)
 * for the caller to draw as its own extra fan-out block below. */
function walkRouterChain(
  combined: RouterEntry[],
  routerSteps: CeligoFlowStep[],
  visited: Set<string>,
): { spineItems: SpineRouterItem[]; fanOutRouter: RouterEntry | undefined } {
  const spineItems: SpineRouterItem[] = [];
  let fanOutRouter: RouterEntry | undefined;
  let current: RouterEntry | undefined = combined[0];

  while (current) {
    visited.add(current.id);
    spineItems.push({ kind: "router", router: current });
    const soleBranch: CeligoRouterBranch | undefined = current.branches.length === 1 ? current.branches[0] : undefined;
    const isPassThrough = !!soleBranch && !!soleBranch.next_router_id;
    if (!isPassThrough) {
      fanOutRouter = current;
      break;
    }
    const branchSteps = stepsForBranch(routerSteps, current, soleBranch, 0);
    if (branchSteps.length === 0) {
      spineItems.push({
        kind: "placeholder",
        routerId: current.id,
        branchId: soleBranch.id,
        branchKey: branchKey(current.id, soleBranch, 0),
      });
    } else {
      for (const s of branchSteps) spineItems.push({ kind: "step", step: s, routerId: current.id, branchId: soleBranch.id });
    }
    const next: RouterEntry | undefined = combined.find((r) => r.id === soleBranch.next_router_id);
    current = next && !visited.has(next.id) ? next : undefined;
  }

  return { spineItems, fanOutRouter };
}

type PendingEdge = { from: string; to: string; curved: boolean; fromStep?: CeligoFlowStep };

export function computeLayout(detail: Pick<CeligoFlowDetail, "steps" | "routers">): Layout {
  if (detail.steps.length === 0) {
    return { nodes: [], edges: [], lanes: [], routers: [], captions: [], width: 0, height: 0, warnings: ["no steps recorded"] };
  }

  const warnings: string[] = [];
  const sources = detail.steps.filter((s) => s.role === "generator").sort(bySequence);
  const topChain = detail.steps.filter((s) => s.role === "processor" && s.router_id === null).sort(bySequence);
  const routerSteps = detail.steps.filter((s) => s.router_id !== null);

  const combined = buildRouterList(detail.routers, routerSteps, warnings);
  const visitedIds = new Set<string>();
  const { spineItems, fanOutRouter } = walkRouterChain(combined, routerSteps, visitedIds);

  const numPrimaryLanes = fanOutRouter ? fanOutRouter.branches.length : 0;
  const lanesTop = MARGIN + LANE_LABEL_H;
  const lanesBottom = lanesTop + (numPrimaryLanes - 1) * LANE_PITCH + BUBBLE_H;
  const lanesCentreY = numPrimaryLanes > 0 ? Math.round((lanesTop + lanesBottom) / 2) : lanesTop;

  // The spine's y is the LOWER of the two constraints, never just the lanes'
  // centre: rank 0's sources are stacked CENTRED on the spine (step 1 below),
  // so a stack taller than one bubble needs the spine pushed down by half the
  // overflow or the topmost source lands above the canvas -- at y=-48 for two
  // sources with no fan-out router, clipped away by the canvas's own
  // `overflow: hidden` sizer with no scroll that could recover it. Deriving
  // it here (rather than clamping each source's y afterwards) keeps the stack
  // centred on the spine, which is what makes the source->spine edge read as
  // a straight line into the middle of the chain.
  const sourcesH = sources.length > 0 ? sources.length * BUBBLE_H + (sources.length - 1) * GAP_X : 0;
  const minSpineY = sources.length > 0 ? lanesTop + Math.ceil((sourcesH - BUBBLE_H) / 2) : lanesTop;
  const spineY = Math.max(lanesCentreY, minSpineY);

  const nodes: LayoutNode[] = [];
  const lanes: LaneLabel[] = [];
  const pendingEdges: PendingEdge[] = [];
  let x = MARGIN;

  // 1. Sources -- stacked vertically in rank 0, centred on the spine.
  const sourceNodeIds: string[] = [];
  if (sources.length > 0) {
    const totalH = sources.length * BUBBLE_H + (sources.length - 1) * GAP_X;
    let y = spineY + BUBBLE_H / 2 - totalH / 2;
    for (const s of sources) {
      nodes.push({ id: s.id, type: "step", x, y, w: BUBBLE_W, h: BUBBLE_H, stepId: s.id });
      sourceNodeIds.push(s.id);
      y += BUBBLE_H + GAP_X;
    }
    x += BUBBLE_W + GAP_X;
  }

  // 2. Top-level chain + 3. router-chain spine items, one rank each, all on
  // the spine row -- collected into one ordered sequence so the "spine node
  // i -> i+1" edges (rule 7) and the router-node y-centring are a single pass.
  const spineSeq: { id: string; step?: CeligoFlowStep }[] = [];
  for (const s of topChain) {
    nodes.push({ id: s.id, type: "step", x, y: spineY, w: BUBBLE_W, h: BUBBLE_H, stepId: s.id });
    spineSeq.push({ id: s.id, step: s });
    x += BUBBLE_W + GAP_X;
  }
  for (const item of spineItems) {
    if (item.kind === "router") {
      const id = `router:${item.router.id}`;
      nodes.push({ id, type: "router", x, y: spineY + (BUBBLE_H - ROUTER_H) / 2, w: ROUTER_W, h: ROUTER_H, routerId: item.router.id });
      spineSeq.push({ id });
      x += ROUTER_W + GAP_X;
    } else if (item.kind === "step") {
      nodes.push({
        id: item.step.id,
        type: "step",
        x,
        y: spineY,
        w: BUBBLE_W,
        h: BUBBLE_H,
        stepId: item.step.id,
        routerId: item.routerId,
        branchId: item.branchId,
      });
      spineSeq.push({ id: item.step.id, step: item.step });
      x += BUBBLE_W + GAP_X;
    } else {
      const id = `placeholder:${item.branchKey}`;
      nodes.push({ id, type: "placeholder", x, y: spineY, w: BUBBLE_W, h: BUBBLE_H, routerId: item.routerId, branchId: item.branchId });
      spineSeq.push({ id });
      x += BUBBLE_W + GAP_X;
    }
  }
  for (let i = 1; i < spineSeq.length; i++) {
    pendingEdges.push({ from: spineSeq[i - 1].id, to: spineSeq[i].id, curved: false, fromStep: spineSeq[i - 1].step });
  }
  if (sources.length > 0 && spineSeq.length > 0) {
    pendingEdges.push({
      from: sourceNodeIds[sourceNodeIds.length - 1],
      to: spineSeq[0].id,
      curved: false,
      fromStep: sources[sources.length - 1],
    });
  }

  /** Lays out one branch's row (a lane, or a remaining router's own branch
   * row): its steps as consecutive ranks from `startX`, or one placeholder
   * when the branch declares no steps at all (rule 6). When the branch's own
   * `next_router_id` is set, that nested router is appended at the end of
   * the row with its own branches collapsed into a single placeholder --
   * drawn inline, never expanded (rule 3's last clause) -- so a router chain
   * buried inside a lane still terminates in a fixed number of ranks. */
  function layoutBranchRow(owner: RouterEntry, branch: CeligoRouterBranch, laneIndex: number, startX: number, rowY: number): string {
    const ownerRouterId = owner.id;
    const branchSteps = stepsForBranch(routerSteps, owner, branch, laneIndex);
    let cx = startX;
    let firstId: string | undefined;
    let prevId: string | undefined;
    let prevStep: CeligoFlowStep | undefined;

    const advance = (id: string, w: number, step: CeligoFlowStep | undefined) => {
      if (!firstId) firstId = id;
      if (prevId) pendingEdges.push({ from: prevId, to: id, curved: false, fromStep: prevStep });
      prevId = id;
      prevStep = step;
      cx += w + GAP_X;
    };

    if (branchSteps.length === 0) {
      const id = `placeholder:${branchKey(ownerRouterId, branch, laneIndex)}`;
      nodes.push({ id, type: "placeholder", x: cx, y: rowY, w: BUBBLE_W, h: BUBBLE_H, routerId: ownerRouterId, branchId: branch.id, lane: laneIndex });
      advance(id, BUBBLE_W, undefined);
    } else {
      for (const s of branchSteps) {
        nodes.push({
          id: s.id,
          type: "step",
          x: cx,
          y: rowY,
          w: BUBBLE_W,
          h: BUBBLE_H,
          stepId: s.id,
          routerId: ownerRouterId,
          branchId: branch.id,
          lane: laneIndex,
        });
        advance(s.id, BUBBLE_W, s);
      }
    }

    if (branch.next_router_id) {
      const nested = combined.find((r) => r.id === branch.next_router_id);
      if (nested && !visitedIds.has(nested.id)) {
        visitedIds.add(nested.id);
        warnings.push(WARN_NESTED_ROUTER_INLINE);
        const routerNodeId = `router:${nested.id}`;
        nodes.push({
          id: routerNodeId,
          type: "router",
          x: cx,
          y: rowY + (BUBBLE_H - ROUTER_H) / 2,
          w: ROUTER_W,
          h: ROUTER_H,
          routerId: nested.id,
          lane: laneIndex,
        });
        advance(routerNodeId, ROUTER_W, undefined);
        const collapseId = `placeholder:${nested.id}:branches`;
        nodes.push({
          id: collapseId,
          type: "placeholder",
          x: cx,
          y: rowY,
          w: BUBBLE_W,
          h: BUBBLE_H,
          routerId: nested.id,
          branchId: null,
          lane: laneIndex,
        });
        advance(collapseId, BUBBLE_W, undefined);
      }
    }

    return firstId!;
  }

  // 4. Primary fan-out lanes -- rows, in declared order, LANE_PITCH apart.
  let laneBottom = spineY + BUBBLE_H;
  if (fanOutRouter) {
    const laneStartX = x;
    const fanOutNodeId = `router:${fanOutRouter.id}`;
    fanOutRouter.branches.forEach((branch, i) => {
      const laneY = MARGIN + LANE_LABEL_H + i * LANE_PITCH;
      lanes.push({
        routerId: fanOutRouter.id,
        branchId: branchKey(fanOutRouter.id, branch, i),
        name: branch.name,
        ruleCount: branch.rule_count,
        order: branch.order,
        x: laneStartX,
        y: laneY - LANE_LABEL_H,
      });
      const firstId = layoutBranchRow(fanOutRouter, branch, i, laneStartX, laneY);
      pendingEdges.push({ from: fanOutNodeId, to: firstId, curved: true });
      laneBottom = Math.max(laneBottom, laneY + BUBBLE_H);
    });
  }

  // 5. Remaining undeclared/unchained routers -- stacked below the last
  // lane as their own extra fan-out blocks (point 4 of the algorithm).
  // Computed AFTER the primary lanes render (not from walkRouterChain's
  // earlier snapshot): a lane's own `next_router_id` nesting (rule 3's last
  // clause) can consume one of these routers inline first, and the guard
  // inside the loop covers a later entry doing the same to an earlier one --
  // either way a router already drawn inline must never be drawn again here.
  let extraY = laneBottom + GAP_X;
  for (const rem of combined.filter((r) => !visitedIds.has(r.id))) {
    if (visitedIds.has(rem.id)) continue;
    visitedIds.add(rem.id);
    warnings.push(WARN_ROUTER_ORDER_UNVERIFIED);
    const routerNodeId = `router:${rem.id}`;
    nodes.push({ id: routerNodeId, type: "router", x: MARGIN, y: extraY + (BUBBLE_H - ROUTER_H) / 2, w: ROUTER_W, h: ROUTER_H, routerId: rem.id });
    const blockStartX = MARGIN + ROUTER_W + GAP_X;
    rem.branches.forEach((branch, i) => {
      const laneY = extraY + i * LANE_PITCH;
      lanes.push({
        routerId: rem.id,
        branchId: branchKey(rem.id, branch, i),
        name: branch.name,
        ruleCount: branch.rule_count,
        order: branch.order,
        x: blockStartX,
        y: laneY - LANE_LABEL_H,
      });
      const firstId = layoutBranchRow(rem, branch, i, blockStartX, laneY);
      pendingEdges.push({ from: routerNodeId, to: firstId, curved: true });
    });
    const blockLanes = Math.max(rem.branches.length, 1);
    extraY += (blockLanes - 1) * LANE_PITCH + BUBBLE_H + GAP_X;
  }

  // 6. Resolve edges now that every node's final geometry is known.
  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const edges: LayoutEdge[] = pendingEdges.map((e) => {
    const from = nodeById.get(e.from)!;
    const to = nodeById.get(e.to)!;
    const dashed = e.fromStep?.proceed_on_failure === true;
    return {
      id: `${e.from}->${e.to}`,
      from: e.from,
      to: e.to,
      x1: from.x + from.w,
      y1: from.y + from.h / 2,
      x2: to.x,
      y2: to.y + to.h / 2,
      curved: e.curved,
      dashed,
      ...(dashed ? { label: "continues on failure" } : {}),
    };
  });

  const maxX = Math.max(...nodes.map((n) => n.x + n.w));
  const maxY = Math.max(...nodes.map((n) => n.y + n.h));

  return { nodes, edges, lanes, routers: combined, captions: [], width: maxX + MARGIN, height: maxY + MARGIN, warnings };
}
