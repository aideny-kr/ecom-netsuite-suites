import { describe, expect, it } from "vitest";
import { computeLayout, BUBBLE_W, BUBBLE_H, LANE_PITCH, MARGIN, LANE_LABEL_H } from "../layout";
import type { CeligoFlowDetail, CeligoFlowStep, CeligoRouter, CeligoRouterBranch } from "@/hooks/use-celigo-flows";

// Fixture-matrix helpers -- deliberately minimal, filling every required
// CeligoFlowStep/CeligoRouter/CeligoRouterBranch field with an inert default
// so each test case only spells out what it actually varies.

function step(overrides: Partial<CeligoFlowStep> & Pick<CeligoFlowStep, "id" | "celigo_id" | "role" | "sequence">): CeligoFlowStep {
  return {
    router_id: null,
    branch_id: null,
    branch_key: "$root",
    adaptor_type: null,
    connection_celigo_id: null,
    reference_name: null,
    filter_json: null,
    mapping_json: null,
    proceed_on_failure: null,
    skip_retries: null,
    kind: overrides.role === "generator" ? "source" : "destination",
    record_type: null,
    operation: null,
    search_id: null,
    attachments: [],
    error_count: 0,
    ...overrides,
  };
}

function branch(overrides: Partial<CeligoRouterBranch> & Pick<CeligoRouterBranch, "id">): CeligoRouterBranch {
  return { name: null, rule_count: 0, next_router_id: null, order: 0, declared_step_count: 0, ...overrides };
}

function routerDef(overrides: Partial<CeligoRouter> & Pick<CeligoRouter, "id" | "branches">): CeligoRouter {
  return { name: null, route_records_to: null, route_records_using: null, has_script_slot: false, ...overrides };
}

function mk(steps: CeligoFlowStep[], routers: CeligoRouter[] = []): Pick<CeligoFlowDetail, "steps" | "routers"> {
  return { steps, routers };
}

describe("computeLayout — deterministic layered layout (pure)", () => {
  it("(a) single source -> single destination: 2 nodes, 1 edge, same y", () => {
    const layout = computeLayout(
      mk([
        step({ id: "src", celigo_id: "c1", role: "generator", sequence: 0 }),
        step({ id: "dst", celigo_id: "c2", role: "processor", sequence: 0 }),
      ]),
    );
    expect(layout.nodes).toHaveLength(2);
    expect(layout.edges).toHaveLength(1);
    const src = layout.nodes.find((n) => n.id === "src")!;
    const dst = layout.nodes.find((n) => n.id === "dst")!;
    expect(src.y).toBe(dst.y);
    expect(layout.edges[0]).toMatchObject({ from: "src", to: "dst" });
  });

  it("(b) two sources stack vertically at equal x, and EVERY source feeds the chain (item 18)", () => {
    // Codex fix wave, item 18. Only the LAST source (by sequence) got an edge
    // into the spine, so a flow with two inputs drew one of them floating
    // with nothing leaving it — a picture that says that source feeds
    // nothing. Every source reaches the first spine node.
    const layout = computeLayout(
      mk([
        step({ id: "src1", celigo_id: "c1", role: "generator", sequence: 0 }),
        step({ id: "src2", celigo_id: "c2", role: "generator", sequence: 1 }),
        step({ id: "dst", celigo_id: "c3", role: "processor", sequence: 0 }),
      ]),
    );
    const src1 = layout.nodes.find((n) => n.id === "src1")!;
    const src2 = layout.nodes.find((n) => n.id === "src2")!;
    expect(src1.x).toBe(src2.x);
    expect(src1.y).not.toBe(src2.y);

    const intoDst = layout.edges.filter((e) => e.to === "dst");
    expect(intoDst.map((e) => e.from).sort()).toEqual(["src1", "src2"]);
    expect(new Set(intoDst.map((e) => e.id)).size).toBe(2);
  });

  it("(b2) each source's own edge carries that source's proceed_on_failure (item 18)", () => {
    const layout = computeLayout(
      mk([
        step({ id: "src1", celigo_id: "c1", role: "generator", sequence: 0, proceed_on_failure: true }),
        step({ id: "src2", celigo_id: "c2", role: "generator", sequence: 1 }),
        step({ id: "dst", celigo_id: "c3", role: "processor", sequence: 0 }),
      ]),
    );
    expect(layout.edges.find((e) => e.from === "src1")!.dashed).toBe(true);
    expect(layout.edges.find((e) => e.from === "src2")!.dashed).toBe(false);
  });

  it("(b3) every source reaches the fan-out router when that is the first spine node (item 18)", () => {
    const layout = computeLayout(
      mk(
        [
          step({ id: "src1", celigo_id: "c1", role: "generator", sequence: 0 }),
          step({ id: "src2", celigo_id: "c2", role: "generator", sequence: 1 }),
          step({ id: "b1s", celigo_id: "c3", role: "processor", router_id: "r", branch_id: "b1", sequence: 0 }),
        ],
        [routerDef({ id: "r", branches: [branch({ id: "b1", order: 0 })] })],
      ),
    );
    expect(layout.edges.filter((e) => e.to === "router:r").map((e) => e.from).sort()).toEqual(["src1", "src2"]);
  });

  it("(c) a top-level chain of 3 processor steps: x strictly increasing, 2 edges", () => {
    const layout = computeLayout(
      mk([
        step({ id: "p1", celigo_id: "c1", role: "processor", sequence: 0 }),
        step({ id: "p2", celigo_id: "c2", role: "processor", sequence: 1 }),
        step({ id: "p3", celigo_id: "c3", role: "processor", sequence: 2 }),
      ]),
    );
    const xs = ["p1", "p2", "p3"].map((id) => layout.nodes.find((n) => n.id === id)!.x);
    expect(xs[0]).toBeLessThan(xs[1]);
    expect(xs[1]).toBeLessThan(xs[2]);
    expect(layout.edges).toHaveLength(2);
  });

  it("(d) one fan-out router with 2 lanes of 2, in DECLARED order (not id-sorted)", () => {
    // "zIntl" sorts lexically AFTER "aInc" -- proves declared array order wins, not a re-sort.
    const layout = computeLayout(
      mk(
        [
          step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
          step({ id: "z_lookup", celigo_id: "c1", role: "processor", router_id: "r", branch_id: "zIntl", sequence: 0 }),
          step({ id: "z_add", celigo_id: "c2", role: "processor", router_id: "r", branch_id: "zIntl", sequence: 1 }),
          step({ id: "a_lookup", celigo_id: "c3", role: "processor", router_id: "r", branch_id: "aInc", sequence: 0 }),
          step({ id: "a_add", celigo_id: "c4", role: "processor", router_id: "r", branch_id: "aInc", sequence: 1 }),
        ],
        [
          routerDef({
            id: "r",
            branches: [
              branch({ id: "zIntl", name: "Framework Intl", rule_count: 1, order: 0 }),
              branch({ id: "aInc", name: "Framework Inc", rule_count: 1, order: 1 }),
            ],
          }),
        ],
      ),
    );
    expect(layout.nodes.find((n) => n.id === "router:r")).toBeTruthy();
    expect(layout.lanes.map((l) => l.name)).toEqual(["Framework Intl", "Framework Inc"]);
    expect(layout.lanes.map((l) => l.ruleCount)).toEqual([1, 1]);
    expect(layout.lanes[1].y - layout.lanes[0].y).toBe(LANE_PITCH);
    const curved = layout.edges.filter((e) => e.curved && e.from === "router:r");
    expect(curved).toHaveLength(2);
    expect(curved.map((e) => e.to).sort()).toEqual(["a_lookup", "z_lookup"].sort());
  });

  it("(e) an empty declared branch renders a placeholder node", () => {
    const layout = computeLayout(
      mk(
        [
          step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
          step({ id: "b1s", celigo_id: "c1", role: "processor", router_id: "r", branch_id: "b1", sequence: 0 }),
        ],
        [
          routerDef({
            id: "r",
            branches: [branch({ id: "b1", name: "A", order: 0 }), branch({ id: "b2", name: "B", order: 1 })],
          }),
        ],
      ),
    );
    const ph = layout.nodes.find((n) => n.id === "placeholder:b2");
    expect(ph).toMatchObject({ type: "placeholder", w: BUBBLE_W, h: BUBBLE_H, branchId: "b2" });
  });

  it("(e2) two branches that declare no id collapse into ONE lane, not two invented ones (item 19 / R19a)", () => {
    // Final-review finding I2 first stopped the SAME step being drawn into
    // every unnamed lane (duplicate node/edge/lane ids, which React silently
    // renders as one). Codex fix wave, item 19 (ruling R19a) goes further:
    // drawing two unnamed lanes at all invents a topology the sync never
    // supplied — "A got this step, B got none" is a claim, and the branch
    // NAMES Celigo did give ("A", "B") make it read as a confident one. With
    // ids missing, the honest picture is one lane holding every unattributed
    // step, labelled as exactly that.
    const layout = computeLayout(
      mk(
        [
          step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
          step({ id: "s1", celigo_id: "c1", role: "processor", router_id: "r", branch_id: null, sequence: 0 }),
          step({ id: "s2", celigo_id: "c2", role: "processor", router_id: "r", branch_id: null, sequence: 1 }),
        ],
        [
          routerDef({
            id: "r",
            branches: [branch({ id: null, name: "A", order: 0 }), branch({ id: null, name: "B", order: 1 })],
          }),
        ],
      ),
    );
    const nodeIds = layout.nodes.map((n) => n.id);
    expect(new Set(nodeIds).size, `duplicate node ids: ${JSON.stringify(nodeIds)}`).toBe(nodeIds.length);
    const edgeIds = layout.edges.map((e) => e.id);
    expect(new Set(edgeIds).size, `duplicate edge ids: ${JSON.stringify(edgeIds)}`).toBe(edgeIds.length);

    // ONE lane, flagged as a merge of both id-less branches — and no lane
    // carries either invented name.
    expect(layout.lanes).toHaveLength(1);
    expect(layout.lanes[0].mergedBranchCount).toBe(2);
    expect(layout.lanes.map((l) => l.name)).toEqual([null]);
    expect(layout.warnings).toContain("2 branches have no id — steps not attributable");

    // Both unattributed steps live in that single lane; nothing is duplicated
    // and no empty placeholder lane is invented for the second branch.
    expect(layout.nodes.filter((n) => n.id === "s1")).toHaveLength(1);
    expect(layout.nodes.filter((n) => n.id === "s2")).toHaveLength(1);
    expect(layout.nodes.find((n) => n.id === "s1")!.lane).toBe(0);
    expect(layout.nodes.find((n) => n.id === "s2")!.lane).toBe(0);
    expect(layout.nodes.filter((n) => n.type === "placeholder")).toHaveLength(0);
  });

  it("(e3) a SINGLE id-less branch is left alone — there is nothing to merge (item 19)", () => {
    const layout = computeLayout(
      mk(
        [
          step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
          step({ id: "s1", celigo_id: "c1", role: "processor", router_id: "r", branch_id: null, sequence: 0 }),
          step({ id: "s2", celigo_id: "c2", role: "processor", router_id: "r", branch_id: "b2", sequence: 0 }),
        ],
        [
          routerDef({
            id: "r",
            branches: [branch({ id: null, name: "A", order: 0 }), branch({ id: "b2", name: "B", order: 1 })],
          }),
        ],
      ),
    );
    expect(layout.lanes).toHaveLength(2);
    expect(layout.lanes.map((l) => l.name)).toEqual(["A", "B"]);
    expect(layout.lanes.every((l) => l.mergedBranchCount === undefined)).toBe(true);
    expect(layout.warnings.some((w) => w.includes("no id"))).toBe(false);
  });

  it("(f) the same celigo_id in two branches still produces two distinct step nodes", () => {
    const layout = computeLayout(
      mk(
        [
          step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
          step({ id: "s1", celigo_id: "dup", role: "processor", router_id: "r", branch_id: "b1", sequence: 0 }),
          step({ id: "s2", celigo_id: "dup", role: "processor", router_id: "r", branch_id: "b2", sequence: 0 }),
        ],
        [routerDef({ id: "r", branches: [branch({ id: "b1", order: 0 }), branch({ id: "b2", order: 1 })] })],
      ),
    );
    expect(layout.nodes.find((n) => n.id === "s1")).toMatchObject({ type: "step", stepId: "s1" });
    expect(layout.nodes.find((n) => n.id === "s2")).toMatchObject({ type: "step", stepId: "s2" });
  });

  it("(g) 0 steps: empty layout with the standard warning", () => {
    const layout = computeLayout(mk([]));
    expect(layout.nodes).toEqual([]);
    expect(layout.edges).toEqual([]);
    expect(layout.warnings).toEqual(["no steps recorded"]);
    expect(layout.width).toBe(0);
    expect(layout.height).toBe(0);
  });

  it("(h) the Multi-Subsidiary chain: source, router:r1, lookup, router:r2 with increasing x, then 2 lanes of 4", () => {
    const branchSteps = (branchId: string) =>
      [0, 1, 2, 3].map((seq) =>
        step({ id: `${branchId}_${seq}`, celigo_id: `${branchId}_c${seq}`, role: "processor", router_id: "r2", branch_id: branchId, sequence: seq }),
      );
    const layout = computeLayout(
      mk(
        [
          step({ id: "src", celigo_id: "src_c", role: "generator", sequence: 0 }),
          step({ id: "lkp", celigo_id: "lkp_c", role: "processor", router_id: "r1", branch_id: "b0", sequence: 0 }),
          ...branchSteps("bIntl"),
          ...branchSteps("bInc"),
        ],
        [
          routerDef({ id: "r1", branches: [branch({ id: "b0", next_router_id: "r2", order: 0 })] }),
          routerDef({
            id: "r2",
            branches: [
              branch({ id: "bIntl", name: "Framework Intl", order: 0 }),
              branch({ id: "bInc", name: "Framework Inc", order: 1 }),
            ],
          }),
        ],
      ),
    );
    const ids = ["src", "router:r1", "lkp", "router:r2"];
    const xs = ids.map((id) => layout.nodes.find((n) => n.id === id)!.x);
    for (let i = 1; i < xs.length; i++) expect(xs[i]).toBeGreaterThan(xs[i - 1]);
    expect(layout.lanes).toHaveLength(2);
    expect(layout.nodes.filter((n) => n.type === "step" && n.branchId === "bIntl")).toHaveLength(4);
    expect(layout.nodes.filter((n) => n.type === "step" && n.branchId === "bInc")).toHaveLength(4);
  });

  it("(i) two undeclared routers (steps only, no detail.routers): both drawn, warning pushed", () => {
    const layout = computeLayout(
      mk([
        step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
        step({ id: "r1s", celigo_id: "c1", role: "processor", router_id: "r1", branch_id: "b1", sequence: 0 }),
        step({ id: "r2s", celigo_id: "c2", role: "processor", router_id: "r2", branch_id: "b1", sequence: 0 }),
      ]),
    );
    expect(layout.nodes.find((n) => n.id === "router:r1")).toBeTruthy();
    expect(layout.nodes.find((n) => n.id === "router:r2")).toBeTruthy();
    expect(layout.warnings).toContain("router order unverified");
  });

  it("(i4) the router-order warning is pushed once per router, not once per pass over it", () => {
    // An unchained synthetic router was warned about twice -- once when it
    // was invented, once again when it was drawn as a "remaining" block --
    // and the canvas joins warnings with " · ", so the strip repeated itself.
    const layout = computeLayout(
      mk([
        step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
        step({ id: "r1s", celigo_id: "c1", role: "processor", router_id: "r1", branch_id: "b1", sequence: 0 }),
        step({ id: "r2s", celigo_id: "c2", role: "processor", router_id: "r2", branch_id: "b1", sequence: 0 }),
      ]),
    );
    expect(layout.warnings.filter((w) => w === "router order unverified")).toHaveLength(2);
  });

  it("(i2) an undeclared router's steps with NO branch_id still get a node, and the gap is named", () => {
    // Gate fix wave, item 3. A synthetic router's branches were built only
    // from the non-null `branch_id`s its steps carried, so a step that named
    // the router but no branch belonged to no lane at all -- it vanished
    // with no node, no placeholder and no warning, while the router it
    // pointed at was drawn as if complete.
    const layout = computeLayout(
      mk([
        step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
        step({ id: "unbranched", celigo_id: "c1", role: "processor", router_id: "rX", branch_id: null, sequence: 0 }),
        step({ id: "branched", celigo_id: "c2", role: "processor", router_id: "rX", branch_id: "b1", sequence: 1 }),
      ]),
    );

    expect(layout.nodes.find((n) => n.id === "unbranched")).toBeTruthy();
    expect(layout.nodes.find((n) => n.id === "branched")).toBeTruthy();
    expect(layout.warnings).toContain("undeclared router: 1 step(s) without a branch");
  });

  it("(i3) an undeclared router whose steps ALL lack a branch_id still draws them", () => {
    const layout = computeLayout(
      mk([
        step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
        step({ id: "u1", celigo_id: "c1", role: "processor", router_id: "rY", branch_id: null, sequence: 0 }),
        step({ id: "u2", celigo_id: "c2", role: "processor", router_id: "rY", branch_id: null, sequence: 1 }),
      ]),
    );

    expect(layout.nodes.filter((n) => n.type === "step" && n.routerId === "rY").map((n) => n.id).sort()).toEqual([
      "u1",
      "u2",
    ]);
    expect(layout.warnings).toContain("undeclared router: 2 step(s) without a branch");
  });

  it("self-review: a declared router chained-to from INSIDE a fan-out lane is drawn once, not duplicated as an extra 'remaining' block", () => {
    // r_fanout is the chain's entry point and is itself the fan-out router
    // (2 branches). Branch bB's next_router_id nests router r3 inline at the
    // end of that lane (rule 3's last clause). r3 is never touched by the
    // top-level spine walk (it only has one branch and no steps of its own
    // outside the nested reference), so before rendering the lanes it would
    // still look "unchained" -- the bug this test guards against is treating
    // that stale snapshot as authoritative and drawing r3 a second time as
    // its own extra fan-out block below the primary lanes.
    const layout = computeLayout(
      mk(
        [
          step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
          step({ id: "a1", celigo_id: "ca1", role: "processor", router_id: "r_fanout", branch_id: "bA", sequence: 0 }),
          step({ id: "b1", celigo_id: "cb1", role: "processor", router_id: "r_fanout", branch_id: "bB", sequence: 0 }),
          step({ id: "c1", celigo_id: "cc1", role: "processor", router_id: "r3", branch_id: "bC", sequence: 0 }),
        ],
        [
          routerDef({
            id: "r_fanout",
            branches: [
              branch({ id: "bA", order: 0 }),
              branch({ id: "bB", order: 1, next_router_id: "r3" }),
            ],
          }),
          routerDef({ id: "r3", branches: [branch({ id: "bC", order: 0 })] }),
        ],
      ),
    );
    expect(layout.nodes.filter((n) => n.id === "router:r3")).toHaveLength(1);

    // Codex fix wave, item 20. r3's branch used to collapse into a single
    // "no steps declared" placeholder, so `c1` — a real, synced step with its
    // own errors and scripts — had no node at all: unreachable, uninspectable,
    // and counted by the header while missing from the picture. Its branches
    // are expanded as their own lanes below the primary block instead.
    expect(layout.nodes.filter((n) => n.id === "placeholder:r3:branches")).toHaveLength(0);
    const c1 = layout.nodes.find((n) => n.id === "c1");
    expect(c1, "the nested router's own step must have a node").toBeTruthy();
    expect(c1).toMatchObject({ type: "step", stepId: "c1", routerId: "r3", branchId: "bC" });

    // A lane of its own, below every primary lane, with a curved edge from
    // the nested router into it — and the branch's own last step still feeds
    // that router.
    const nestedLane = layout.lanes.find((l) => l.routerId === "r3")!;
    expect(nestedLane).toBeTruthy();
    expect(nestedLane.y).toBeGreaterThan(Math.max(...layout.lanes.filter((l) => l.routerId === "r_fanout").map((l) => l.y)));
    expect(layout.edges.some((e) => e.from === "router:r3" && e.to === "c1" && e.curved)).toBe(true);
    expect(layout.edges.some((e) => e.from === "b1" && e.to === "router:r3")).toBe(true);

    // Nothing overlaps: every node's box is inside the reported canvas.
    expect(layout.height).toBeGreaterThanOrEqual(Math.max(...layout.nodes.map((n) => n.y + n.h)));
  });

  it("item 20: a nested router chain terminates instead of looping forever", () => {
    // Two routers that name each other. The visited set is what stops the
    // expansion; without it this recurses until the stack goes.
    const layout = computeLayout(
      mk(
        [
          step({ id: "src", celigo_id: "c0", role: "generator", sequence: 0 }),
          step({ id: "a1", celigo_id: "ca1", role: "processor", router_id: "rA", branch_id: "bA", sequence: 0 }),
          step({ id: "b1", celigo_id: "cb1", role: "processor", router_id: "rA", branch_id: "bLoop", sequence: 0 }),
          step({ id: "c1", celigo_id: "cc1", role: "processor", router_id: "rB", branch_id: "bBack", sequence: 0 }),
        ],
        [
          routerDef({
            id: "rA",
            branches: [branch({ id: "bA", order: 0 }), branch({ id: "bLoop", order: 1, next_router_id: "rB" })],
          }),
          routerDef({ id: "rB", branches: [branch({ id: "bBack", order: 0, next_router_id: "rA" })] }),
        ],
      ),
    );
    expect(layout.nodes.filter((n) => n.id === "router:rA")).toHaveLength(1);
    expect(layout.nodes.filter((n) => n.id === "router:rB")).toHaveLength(1);
    expect(layout.nodes.filter((n) => n.id === "c1")).toHaveLength(1);
    const nodeIds = layout.nodes.map((n) => n.id);
    expect(new Set(nodeIds).size).toBe(nodeIds.length);
  });

  it("(j) proceed_on_failure on the FROM step makes its outgoing edge dashed with a label", () => {
    const layout = computeLayout(
      mk([
        step({ id: "p1", celigo_id: "c1", role: "processor", sequence: 0, proceed_on_failure: true }),
        step({ id: "p2", celigo_id: "c2", role: "processor", sequence: 1 }),
      ]),
    );
    const edge = layout.edges.find((e) => e.from === "p1" && e.to === "p2")!;
    expect(edge.dashed).toBe(true);
    expect(edge.label).toBe("continues on failure");
  });

  it("(k) several sources and NO fan-out router: nothing is laid out above the canvas top", () => {
    // Gate fix wave, item 2. With no fan-out router the spine used to sit at
    // the canvas top (`lanesTop`, y=44) while the source stack was centred ON
    // it, so a stack taller than one bubble reached above y=0 -- clipped away
    // by the canvas's own `overflow: hidden` sizer, with no scroll to recover
    // it. Two sources put the first bubble at y=-48.
    const layout = computeLayout(
      mk([
        step({ id: "src1", celigo_id: "c1", role: "generator", sequence: 0 }),
        step({ id: "src2", celigo_id: "c2", role: "generator", sequence: 1 }),
        step({ id: "dst", celigo_id: "c3", role: "processor", sequence: 0 }),
      ]),
    );

    const above = layout.nodes.filter((n) => n.y < MARGIN + LANE_LABEL_H).map((n) => `${n.id}@y=${n.y}`);
    expect(above).toEqual([]);
    expect(layout.height).toBeGreaterThanOrEqual(Math.max(...layout.nodes.map((n) => n.y + n.h)));
  });

  it("(k2) a taller source stack still clears the top, and the spine stays centred on it", () => {
    const layout = computeLayout(
      mk([
        step({ id: "s1", celigo_id: "c1", role: "generator", sequence: 0 }),
        step({ id: "s2", celigo_id: "c2", role: "generator", sequence: 1 }),
        step({ id: "s3", celigo_id: "c3", role: "generator", sequence: 2 }),
        step({ id: "s4", celigo_id: "c4", role: "generator", sequence: 3 }),
        step({ id: "dst", celigo_id: "c5", role: "processor", sequence: 0 }),
      ]),
    );

    const above = layout.nodes.filter((n) => n.y < MARGIN + LANE_LABEL_H).map((n) => `${n.id}@y=${n.y}`);
    expect(above).toEqual([]);
    // The destination still sits on the vertical centre of the source stack.
    const ys = ["s1", "s2", "s3", "s4"].map((id) => layout.nodes.find((n) => n.id === id)!.y);
    const dst = layout.nodes.find((n) => n.id === "dst")!;
    expect(dst.y + BUBBLE_H / 2).toBe((Math.min(...ys) + Math.max(...ys) + BUBBLE_H) / 2);
    expect(layout.height).toBeGreaterThanOrEqual(Math.max(...layout.nodes.map((n) => n.y + n.h)));
  });
});
