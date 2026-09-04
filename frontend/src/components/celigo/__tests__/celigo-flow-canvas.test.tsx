import { render, screen, within, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeAll, afterAll } from "vitest";
import type { CeligoAttachment, CeligoFlowDetail, CeligoFlowStep, CeligoRouter } from "@/hooks/use-celigo-flows";
import { computeLayout } from "../layout";
import { CeligoFlowCanvas, FIT_FLOOR } from "../celigo-flow-canvas";

// Task 15 — the real canvas (mockup screen 3): bubbles, router nodes,
// branch lanes, edges, fit/zoom, selection. `CeligoFlowCanvas` takes
// `detail` as a plain prop (Task 14's contract) — no hooks module to mock.

function makeAttachment(overrides: Partial<CeligoAttachment> = {}): CeligoAttachment {
  return {
    id: "att-1",
    flow_id: "flow-1",
    flow_step_id: null,
    script_id: null,
    script_celigo_id: "script-celigo-1",
    function_name: null,
    json_path: "path",
    site_type: null,
    script_name: null,
    script_size_chars: null,
    script_copies_count: null,
    script_versions_count: null,
    script_version_letter: null,
    script_content_diverged: null,
    ...overrides,
  };
}

function makeStep(overrides: Partial<CeligoFlowStep> & Pick<CeligoFlowStep, "id" | "sequence" | "kind">): CeligoFlowStep {
  return {
    celigo_id: `cel-${overrides.id}`,
    role: overrides.kind === "source" ? "generator" : "processor",
    router_id: null,
    branch_id: null,
    branch_key: "$root",
    adaptor_type: "HTTPExport",
    connection_celigo_id: null,
    reference_name: null,
    filter_json: null,
    mapping_json: null,
    proceed_on_failure: null,
    skip_retries: null,
    record_type: null,
    operation: null,
    search_id: null,
    attachments: [],
    error_count: 0,
    ...overrides,
  };
}

// The same real "New Sales Order to NetSuite - Multi-Subsidiary" shape the
// approved mockup (screen 3) draws — 1 source → router 1 (pass-through
// HTTP lookup) → router 2, two branches ("Framework Intl" / "Framework
// Inc"), each lookup customer → add customer → update customer → add
// salesorder. 10 steps, 2 chained routers, as the task brief dictates.
function branchSteps(branchId: string, prefix: string): CeligoFlowStep[] {
  return [
    makeStep({
      id: `${prefix}-lookup`,
      sequence: 0,
      kind: "lookup",
      router_id: "r2",
      branch_id: branchId,
      adaptor_type: "NetSuiteDistributedExport",
      record_type: "customer",
      search_id: "5090",
    }),
    makeStep({
      id: `${prefix}-add-customer`,
      sequence: 1,
      kind: "destination",
      router_id: "r2",
      branch_id: branchId,
      adaptor_type: "NetSuiteDistributedImport",
      record_type: "customer",
      operation: "add",
      proceed_on_failure: true,
    }),
    makeStep({
      id: `${prefix}-update-customer`,
      sequence: 2,
      kind: "destination",
      router_id: "r2",
      branch_id: branchId,
      adaptor_type: "NetSuiteDistributedImport",
      record_type: "customer",
      operation: "update",
    }),
    makeStep({
      id: `${prefix}-add-so`,
      sequence: 3,
      kind: "destination",
      router_id: "r2",
      branch_id: branchId,
      adaptor_type: "NetSuiteDistributedImport",
      record_type: "salesorder",
      operation: "add",
      attachments: [makeAttachment({ function_name: "preMap", script_version_letter: "C", script_versions_count: 3, script_copies_count: 7, script_content_diverged: true })],
    }),
  ];
}

const STEPS: CeligoFlowStep[] = [
  makeStep({ id: "src", sequence: 0, kind: "source", adaptor_type: "HTTPExport", connection_celigo_id: "648bd44c1234" }),
  makeStep({
    id: "lkp",
    sequence: 1,
    kind: "lookup",
    router_id: "r1",
    branch_id: "b0",
    adaptor_type: "HTTPExport",
    connection_celigo_id: "648bd44c1234",
    attachments: [makeAttachment({ function_name: "preSavePage", script_copies_count: 1, script_versions_count: 1, script_content_diverged: false })],
  }),
  ...branchSteps("bIntl", "intl"),
  ...branchSteps("bInc", "inc"),
];

const ROUTERS: CeligoRouter[] = [
  { id: "r1", name: null, route_records_to: null, route_records_using: null, has_script_slot: false, branches: [{ id: "b0", name: null, rule_count: 0, next_router_id: "r2", order: 0, declared_step_count: 1 }] },
  {
    id: "r2",
    name: null,
    route_records_to: "first_matching_branch",
    route_records_using: "input_filters",
    has_script_slot: false,
    branches: [
      { id: "bIntl", name: "Framework Intl", rule_count: 1, next_router_id: null, order: 0, declared_step_count: 4 },
      { id: "bInc", name: "Framework Inc", rule_count: 1, next_router_id: null, order: 1, declared_step_count: 4 },
    ],
  },
];

function makeDetail(overrides: Partial<CeligoFlowDetail> = {}): CeligoFlowDetail {
  return {
    id: "flow-1",
    integration_id: "int-1",
    celigo_id: "cel-flow-1",
    name: "New Sales Order to NetSuite - Multi-Subsidiary",
    disabled: false,
    schedule: null,
    timezone: null,
    last_executed_at: null,
    source_id: null,
    ai_description_summary: null,
    ai_description_detailed: null,
    celigo_last_modified: null,
    steps: STEPS,
    unassigned_attachments: [],
    routers: ROUTERS,
    celigo_open_error_count: 0,
    last_error_at: null,
    error_count: 0,
    signature_count: 0,
    errors_checked_at: null,
    ...overrides,
  };
}

const WRAP_WIDTH = 1200;
let clientWidthDescriptor: PropertyDescriptor | undefined;

beforeAll(() => {
  clientWidthDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, value: WRAP_WIDTH });
});

afterAll(() => {
  if (clientWidthDescriptor) Object.defineProperty(HTMLElement.prototype, "clientWidth", clientWidthDescriptor);
});

function clamp(n: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, n));
}

function renderCanvas(detail: CeligoFlowDetail = makeDetail(), extra: { selectedStepId?: string | null; paused?: boolean } = {}) {
  const onSelectStep = vi.fn();
  render(<CeligoFlowCanvas detail={detail} selectedStepId={extra.selectedStepId ?? null} onSelectStep={onSelectStep} paused={extra.paused ?? false} />);
  return { onSelectStep };
}

describe("CeligoFlowCanvas — bubbles, routers, lanes, edges", () => {
  it("renders one bubble per step — 10 for the Multi-Subsidiary fixture", () => {
    renderCanvas();
    expect(document.querySelectorAll('[data-testid^="step-bubble-"]')).toHaveLength(10);
  });

  it("renders both router nodes with the mockup's exact summary copy", () => {
    renderCanvas();
    const r1 = screen.getByTestId("router-node-r1");
    expect(within(r1).getByTestId("router-label")).toHaveTextContent("Router 1");
    expect(within(r1).getByTestId("router-value")).toHaveTextContent("pass-through · 1 branch → chains to router 2");

    const r2 = screen.getByTestId("router-node-r2");
    expect(within(r2).getByTestId("router-label")).toHaveTextContent("Router 2");
    expect(within(r2).getByTestId("router-value")).toHaveTextContent("first matching branch · by input filters · 2 branches");
  });

  it("renders lane labels as 'Branch {n} · {name} · {ruleCount} rule(s)'", () => {
    renderCanvas();
    expect(screen.getByTestId("lane-label-r2-bIntl")).toHaveTextContent("Branch 1 · Framework Intl · 1 rule");
    expect(screen.getByTestId("lane-label-r2-bInc")).toHaveTextContent("Branch 2 · Framework Inc · 1 rule");
  });

  it("renders exactly layout.edges.length SVG paths, and every dashed one carries stroke-dasharray", () => {
    const detail = makeDetail();
    const layout = computeLayout(detail);
    renderCanvas(detail);
    // `svg > path` (not `svg path`) excludes the arrowhead marker's own
    // `<path>`, nested under `<defs><marker>` — only edge paths are direct
    // children of the `<svg>`.
    const paths = document.querySelectorAll("svg > path");
    expect(paths.length).toBe(layout.edges.length);
    const dashedCount = layout.edges.filter((e) => e.dashed).length;
    expect(dashedCount).toBeGreaterThan(0);
    expect(document.querySelectorAll("path[stroke-dasharray]")).toHaveLength(dashedCount);
  });

  it("clicking a bubble selects that step with no tab", () => {
    const { onSelectStep } = renderCanvas();
    fireEvent.click(screen.getByTestId("step-bubble-src"));
    expect(onSelectStep).toHaveBeenCalledWith("src", undefined);
  });

  it("clicking a chip selects the step at the chip's own tab", () => {
    const { onSelectStep } = renderCanvas();
    const bubble = screen.getByTestId("step-bubble-intl-add-so");
    fireEvent.click(within(bubble).getByText("preMap"));
    expect(onSelectStep).toHaveBeenCalledWith("intl-add-so", "scripts");
  });

  it("a selected step's bubble carries data-selected=\"true\"", () => {
    renderCanvas(makeDetail(), { selectedStepId: "src" });
    expect(screen.getByTestId("step-bubble-src")).toHaveAttribute("data-selected", "true");
    expect(screen.getByTestId("step-bubble-lkp")).not.toHaveAttribute("data-selected");
  });

  it("paused dims every bubble to opacity-60", () => {
    renderCanvas(makeDetail(), { paused: true });
    expect(screen.getByTestId("step-bubble-src").className).toMatch(/opacity-60/);
  });

  it("warnings render as an amber caption", () => {
    // A step that references an undeclared router — computeLayout pushes
    // "router order unverified" (see layout.ts) for a router `detail.routers`
    // never declared at all.
    const detail = makeDetail({
      steps: [
        makeStep({ id: "src2", sequence: 0, kind: "source" }),
        makeStep({ id: "undeclared", sequence: 1, kind: "destination", router_id: "ghost", branch_id: "b1" }),
      ],
      routers: [],
    });
    renderCanvas(detail);
    const caption = screen.getByTestId("canvas-warnings");
    expect(caption).toHaveTextContent("router order unverified");
    expect(caption.className).toMatch(/amber/);
  });

  it("draws a neutral node for a router the flow never declared, instead of an arrow into a gap", () => {
    // Final-review finding I4. `computeLayout` synthesises a router for any
    // `router_id` a step references that `detail.routers` never declared, and
    // reserves canvas space for it — but the canvas looked routers up in a map
    // built from `detail.routers` alone, so `RouterNode` returned null and the
    // reserved rank rendered as blank space with an edge pointing into it.
    const detail = makeDetail({
      steps: [
        makeStep({ id: "src2", sequence: 0, kind: "source" }),
        makeStep({ id: "undeclared", sequence: 1, kind: "destination", router_id: "ghost", branch_id: "b1" }),
      ],
      routers: [],
    });
    renderCanvas(detail);
    const ghost = screen.getByTestId("router-node-ghost");
    expect(within(ghost).getByTestId("router-label")).toHaveTextContent("Router 1");
    expect(within(ghost).getByTestId("router-value")).toHaveTextContent("undeclared · 1 branch");
  });

  // Codex fix wave, item 19 (ruling R19a). The layout collapses several
  // id-less branches into ONE lane; the caption has to say so rather than
  // print "Branch 1 · Unnamed · 0 rules", which reads as one specific branch
  // that happens to have no name.
  it("item 19: a merged id-less lane says how many branches it stands for", () => {
    const detail = makeDetail({
      steps: [
        makeStep({ id: "src", sequence: 0, kind: "source" }),
        makeStep({ id: "s1", sequence: 1, kind: "destination", router_id: "r", branch_id: null }),
      ],
      routers: [
        {
          id: "r",
          name: null,
          route_records_to: "branches",
          route_records_using: "filters",
          has_script_slot: false,
          branches: [
            { id: null, name: "A", rule_count: 1, next_router_id: null, order: 0, declared_step_count: 1 },
            { id: null, name: "B", rule_count: 1, next_router_id: null, order: 1, declared_step_count: 1 },
          ],
        },
      ],
    });
    renderCanvas(detail);

    expect(
      screen.getByText("2 branches · steps not attributable (branch ids missing)"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^Branch 1 · A/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Branch 2 · B/)).not.toBeInTheDocument();
  });

  it("renders the legend row with the three chip states", () => {
    renderCanvas();
    const legend = screen.getByTestId("canvas-legend");
    expect(within(legend).getByText("configured · named")).toBeInTheDocument();
    expect(within(legend).getByText("looked, none")).toBeInTheDocument();
    expect(within(legend).getByText("cannot say · not synced")).toBeInTheDocument();
  });
});

describe("CeligoFlowCanvas — fit/zoom", () => {
  it("starts fit-to-width, reading 'fit · NN%' from the mocked wrap clientWidth", () => {
    const detail = makeDetail();
    const layout = computeLayout(detail);
    const expectedPct = Math.round(clamp(WRAP_WIDTH / layout.width, FIT_FLOOR, 1) * 100);
    renderCanvas(detail);
    expect(screen.getByText(`fit · ${expectedPct}%`)).toBeInTheDocument();
  });

  it("the '+' button switches the label to 100%, and '⤢' switches back to fit", () => {
    const detail = makeDetail();
    const layout = computeLayout(detail);
    const expectedPct = Math.round(clamp(WRAP_WIDTH / layout.width, FIT_FLOOR, 1) * 100);
    renderCanvas(detail);
    fireEvent.click(screen.getByRole("button", { name: "Zoom to 100%" }));
    expect(screen.getByText("100%")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Fit to width" }));
    expect(screen.getByText(`fit · ${expectedPct}%`)).toBeInTheDocument();
  });

  // Codex fix wave, item 22 (ruling R19b), floor lowered per this branch's
  // brief (0.6 -> 0.4, so fit-to-width actually fits a chained-router flow in
  // a ~800px canvas). The floor itself stays — below it the bubbles are
  // unreadable and the canvas scrolls instead — but the label must stop
  // calling the result "fit": at the floor the diagram does NOT fit the
  // viewport, and a reader who trusts the word stops scrolling and misses
  // steps.
  it(`item 22: says 'min · ${FIT_FLOOR * 100}%', not 'fit', when the fit scale is clamped at the floor`, () => {
    // A twenty-step top-level chain is well past 3000px wide against a
    // 1200px wrap -- an unclamped fit is comfortably below the new 0.4
    // floor (a 12-step chain, ~42% unclamped, no longer qualifies at this
    // floor -- this fixture is deliberately longer than the minimum needed).
    const long = makeDetail({
      steps: Array.from({ length: 20 }, (_, i) =>
        makeStep({ id: `chain-${i}`, sequence: i, kind: i === 0 ? "source" : "destination" }),
      ),
      routers: [],
    });
    const layout = computeLayout(long);
    expect(WRAP_WIDTH / layout.width).toBeLessThan(FIT_FLOOR);

    renderCanvas(long);
    expect(screen.getByText(`min · ${FIT_FLOOR * 100}%`)).toBeInTheDocument();
    expect(screen.queryByText(/^fit · /)).not.toBeInTheDocument();
  });
});
