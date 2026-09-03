import { describe, expect, it } from "vitest";
import { computeLayout, BUBBLE_W, BUBBLE_H, LANE_PITCH } from "../layout";
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

  it("(b) two sources stack vertically at equal x; the chain edge leaves the LAST source (by sequence)", () => {
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
    const edge = layout.edges.find((e) => e.to === "dst")!;
    expect(edge.from).toBe("src2");
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
    expect(layout.nodes.filter((n) => n.id === "placeholder:r3:branches")).toHaveLength(1);
    // r3's own branch is collapsed, not expanded (rule 3's last clause) --
    // its real step never renders as a node, nested or otherwise.
    expect(layout.nodes.filter((n) => n.id === "c1")).toHaveLength(0);
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
});
