import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import type {
  CeligoFlowDetail,
  CeligoFlowStep,
  CeligoFlowSummary,
  CeligoIntegration,
  CeligoAttachment,
  CeligoRouter,
} from "@/hooks/use-celigo-flows";
import { resolved, pending, errored } from "./query-fixtures";
import { deriveFlowSummary } from "../shared";

// Task 14 — the flow page shell (mockup screen 3): header facts, the
// navigator rail/list, the canvas/inspector slots (Task 15/16 stubs), and
// every non-canvas state (loading, failed, unknown id, empty, paused).
// Mocks the hooks module and the route module the same way Tasks 10/12 do.

const mocks = vi.hoisted(() => ({
  detail: vi.fn(),
  integrationFlows: vi.fn(),
  integrations: vi.fn(),
  syncStatus: vi.fn(),
  flowErrors: vi.fn(),
  script: vi.fn(),
}));

vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoFlowDetail: () => mocks.detail(),
  useCeligoIntegrationFlows: () => mocks.integrationFlows(),
  useCeligoIntegrations: () => mocks.integrations(),
  useCeligoSyncStatus: () => mocks.syncStatus(),
  // Task 16 -- the real `CeligoStepInspector` (no longer the Task 14 stub)
  // calls `useCeligoFlowErrors(detail.id)` unconditionally (even in the
  // resting state, since hooks can't be called conditionally), so this
  // file's mock must cover it too even though none of Task 14's own
  // assertions touch flow errors.
  useCeligoFlowErrors: () => mocks.flowErrors(),
  // Task 17 -- the script drawer (`CeligoScriptDrawer`) is now always
  // mounted (its own `open={!!scriptId}` controls visibility), so it calls
  // `useCeligoScript` on every render of this page, not only when a test
  // sets `scriptId`. Without this the "Escape" test below (which DOES set
  // `routeMocks.scriptId`) would throw calling an undefined mock export.
  useCeligoScript: (scriptId: string | undefined) => mocks.script(scriptId),
}));

const routeMocks = vi.hoisted(() => ({
  integrationId: "int-1" as string | null,
  flowId: "flow-1" as string | null,
  stepId: null as string | null,
  scriptId: null as string | null,
  go: {
    files: vi.fn(),
    integrations: vi.fn(),
    integration: vi.fn(),
    flow: vi.fn(),
    step: vi.fn(),
    script: vi.fn(),
  },
}));

vi.mock("../celigo-route", () => ({
  useCeligoRoute: () => ({
    surface: "celigo" as const,
    view: "tiles" as const,
    integrationId: routeMocks.integrationId,
    tab: "flows" as const,
    flowId: routeMocks.flowId,
    stepId: routeMocks.stepId,
    scriptId: routeMocks.scriptId,
    go: routeMocks.go,
  }),
}));

import { CeligoFlowPage } from "../celigo-flow-page";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// Real wall clock during the test, frozen 21 min after the sync — so
// "checked 21 min ago" (NOW vs. sync) and "21 min before the sync" (sync vs.
// this flow's own last run) are both real, independently-computed facts that
// happen to share the same number the mockup's own flow does not (see the
// task brief — this exact fixture is dictated there).
const NOW = new Date("2026-09-02T18:33:00.000Z");
const SYNCED_AT = "2026-09-02T18:12:00.000Z";
const FLOW_LAST_RUN = "2026-09-02T17:51:00.000Z";

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

function makeStep(overrides: Partial<CeligoFlowStep> = {}): CeligoFlowStep {
  return {
    id: "step",
    celigo_id: "cel-step",
    role: "generator",
    router_id: null,
    branch_id: null,
    branch_key: "",
    sequence: 0,
    adaptor_type: "HTTPExport",
    connection_celigo_id: null,
    reference_name: null,
    filter_json: null,
    mapping_json: null,
    proceed_on_failure: null,
    skip_retries: null,
    kind: "source",
    record_type: null,
    operation: null,
    search_id: null,
    attachments: [],
    error_count: 0,
    ...overrides,
  };
}

// The real "New Sales Order to NetSuite - Multi-Subsidiary" pipeline the
// approved mockup (screen 3) draws: 1 source → router 1 (pass-through HTTP
// lookup, with a shared preSavePage hook) → router 2, two branches
// ("Framework Intl" / "Framework Inc"), each lookup customer → add customer
// → update customer → add salesorder (the last carrying the diverged preMap
// hook, attached at both branches' final step — one script, two sites).
const STEPS: CeligoFlowStep[] = [
  makeStep({ id: "s0", sequence: 0, kind: "source", adaptor_type: "HTTPExport" }),
  makeStep({
    id: "s1",
    sequence: 1,
    kind: "lookup",
    adaptor_type: "HTTPExport",
    router_id: "r1",
    branch_id: "r1b1",
    attachments: [
      makeAttachment({
        id: "a1",
        script_id: "s-presave",
        function_name: "preSavePage",
        script_name: "sales_order_script_v2",
        script_content_diverged: false,
      }),
    ],
  }),
  makeStep({
    id: "s2",
    sequence: 2,
    kind: "lookup",
    adaptor_type: "NetSuiteDistributedExport",
    router_id: "r2",
    branch_id: "b1",
    record_type: "customer",
    search_id: "5090",
  }),
  makeStep({
    id: "s3",
    sequence: 3,
    kind: "destination",
    adaptor_type: "NetSuiteDistributedImport",
    router_id: "r2",
    branch_id: "b1",
    record_type: "customer",
    operation: "add",
  }),
  makeStep({
    id: "s4",
    sequence: 4,
    kind: "destination",
    adaptor_type: "NetSuiteDistributedImport",
    router_id: "r2",
    branch_id: "b1",
    record_type: "customer",
    operation: "update",
  }),
  makeStep({
    id: "s5",
    sequence: 5,
    kind: "destination",
    adaptor_type: "NetSuiteDistributedImport",
    router_id: "r2",
    branch_id: "b1",
    record_type: "salesorder",
    operation: "add",
    attachments: [
      makeAttachment({ id: "a2", script_id: "s-premap", function_name: "preMap", script_content_diverged: true }),
    ],
  }),
  makeStep({
    id: "s6",
    sequence: 6,
    kind: "lookup",
    adaptor_type: "NetSuiteDistributedExport",
    router_id: "r2",
    branch_id: "b2",
    record_type: "customer",
    search_id: "5090",
  }),
  makeStep({
    id: "s7",
    sequence: 7,
    kind: "destination",
    adaptor_type: "NetSuiteDistributedImport",
    router_id: "r2",
    branch_id: "b2",
    record_type: "customer",
    operation: "add",
  }),
  makeStep({
    id: "s8",
    sequence: 8,
    kind: "destination",
    adaptor_type: "NetSuiteDistributedImport",
    router_id: "r2",
    branch_id: "b2",
    record_type: "customer",
    operation: "update",
  }),
  makeStep({
    id: "s9",
    sequence: 9,
    kind: "destination",
    adaptor_type: "NetSuiteDistributedImport",
    router_id: "r2",
    branch_id: "b2",
    record_type: "salesorder",
    operation: "add",
    attachments: [
      makeAttachment({ id: "a3", script_id: "s-premap", function_name: "preMap", script_content_diverged: true }),
    ],
  }),
];

const ROUTERS: CeligoRouter[] = [
  {
    id: "r1",
    name: null,
    route_records_to: null,
    route_records_using: null,
    has_script_slot: false,
    branches: [{ id: "r1b1", name: null, rule_count: 0, next_router_id: "r2", order: 0, declared_step_count: 1 }],
  },
  {
    id: "r2",
    name: null,
    route_records_to: "branches",
    route_records_using: "filters",
    has_script_slot: false,
    branches: [
      { id: "b1", name: "Framework Intl", rule_count: 1, next_router_id: null, order: 0, declared_step_count: 4 },
      { id: "b2", name: "Framework Inc", rule_count: 1, next_router_id: null, order: 1, declared_step_count: 4 },
    ],
  },
];

const AI_TEXT =
  'The "Backfill Sales Order by Order Number - 6/7/22" flow retrieves sales order data from an API endpoint using an HTTP export.';

function makeDetail(overrides: Partial<CeligoFlowDetail> = {}): CeligoFlowDetail {
  return {
    id: "flow-1",
    integration_id: "int-1",
    celigo_id: "66738c3d-flow",
    name: "New Sales Order to NetSuite - Multi-Subsidiary",
    disabled: false,
    schedule: "? 5,20,35,50 0-23 ? * *",
    timezone: "America/Los_Angeles",
    last_executed_at: FLOW_LAST_RUN,
    source_id: "666fc163-old-flow",
    ai_description_summary: null,
    ai_description_detailed: AI_TEXT,
    celigo_last_modified: "2026-09-02T09:00:00.000Z",
    steps: STEPS,
    unassigned_attachments: [],
    routers: ROUTERS,
    celigo_open_error_count: 0,
    last_error_at: null,
    error_count: 0,
    signature_count: 0,
    ...overrides,
  };
}

function makeSibling(overrides: Partial<CeligoFlowSummary> = {}): CeligoFlowSummary {
  return {
    id: "flow-1",
    celigo_id: "cel-flow-1",
    name: "New Sales Order to NetSuite - Multi-Subsidiary",
    disabled: false,
    schedule: "? 5,20,35,50 0-23 ? * *",
    timezone: "America/Los_Angeles",
    last_executed_at: FLOW_LAST_RUN,
    error_count: 0,
    signature_count: 0,
    step_count: 10,
    router_count: 2,
    branch_count: 3,
    lookup_count: 3,
    script_count: 2,
    diverged_family_count: 1,
    writes: [],
    celigo_last_modified: "2026-09-02T09:00:00.000Z",
    ...overrides,
  };
}

const SIBLINGS: CeligoFlowSummary[] = [
  makeSibling(),
  makeSibling({ id: "flow-2", celigo_id: "cel-flow-2", name: "NS > Solidus - Shipping Confirmations v2" }),
  makeSibling({ id: "flow-3", celigo_id: "cel-flow-3", name: "NS - Create Customer Deposits", disabled: true }),
];

function makeIntegration(overrides: Partial<CeligoIntegration> = {}): CeligoIntegration {
  return {
    id: "int-1",
    celigo_id: "600a502a-integration",
    name: "Solidus + NetSuite",
    sandbox: false,
    mode: "settings",
    description: null,
    celigo_last_modified: null,
    flow_count: 20,
    scheduled_count: 9,
    on_demand_count: 6,
    paused_count: 5,
    step_count: 94,
    router_count: 11,
    lookup_count: 24,
    script_count: 30,
    no_run_count: 0,
    error_count: 0,
    signature_count: 0,
    changes_last_24h: 0,
    last_run_at: null,
    writes: [],
    adaptor_families: ["HTTP", "NetSuite"],
    flow_schedules: [],
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  mocks.detail.mockReset().mockReturnValue(resolved(makeDetail()));
  mocks.integrationFlows.mockReset().mockReturnValue(resolved(SIBLINGS));
  mocks.integrations.mockReset().mockReturnValue(resolved([makeIntegration()]));
  mocks.syncStatus.mockReset().mockReturnValue(resolved({ last_synced_at: SYNCED_AT }));
  mocks.flowErrors.mockReset().mockReturnValue(resolved({ flow_id: "flow-1", status: "open" as const, total: 0, groups: [] }));
  // Task 17 -- a real script fixture (WITH content), even though most of
  // this file's own tests never open the drawer (`routeMocks.scriptId`
  // defaults to null below, so `CeligoScriptDrawer` stays closed and never
  // renders this data at all). Its own `content` is a marker distinctive
  // enough that if it ever leaked while closed, the regression test below
  // would catch it.
  mocks.script.mockReset().mockReturnValue(
    resolved({
      id: "script-x",
      dedup_key: "dk-x",
      name: "ns_sales_order_premap",
      content: "// UNIQUE_SCRIPT_MARKER_FOR_LEAK_TEST\nfunction preMap() {}",
      content_hash: "hash-x",
      copies_count: 1,
      attachment_count: 1,
      integration_count: 1,
      content_diverged: false,
      used_by: [],
    }),
  );
  routeMocks.integrationId = "int-1";
  routeMocks.flowId = "flow-1";
  routeMocks.stepId = null;
  routeMocks.scriptId = null;
  routeMocks.go.files.mockReset();
  routeMocks.go.integrations.mockReset();
  routeMocks.go.integration.mockReset();
  routeMocks.go.flow.mockReset();
  routeMocks.go.step.mockReset();
  routeMocks.go.script.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("CeligoFlowPage — header", () => {
  it("renders the error/schedule pills, name, full facts strip, summary, AI block, and the Open-in-Celigo/Copy-link actions", () => {
    Object.defineProperty(window.navigator, "clipboard", {
      value: { writeText: vi.fn() },
      configurable: true,
    });

    const { container } = wrap(<CeligoFlowPage />);

    expect(screen.getByText(/0 open errors/)).toBeInTheDocument();
    expect(container.textContent).toContain("checked 21 min ago");
    expect(screen.getByText("on time")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "New Sales Order to NetSuite - Multi-Subsidiary" }),
    ).toBeInTheDocument();

    expect(container.textContent).toContain("every 15 min");
    expect(container.textContent).toContain("? 5,20,35,50 0…23 ? * *");
    expect(container.textContent).toContain("America/Los_Angeles");
    expect(container.textContent).toContain("last ran 17:51 UTC · 21 min before the sync");
    expect(container.textContent).toContain("10 steps · 2 routers · 3 branches · 3 lookups");
    expect(container.textContent).toContain("writes salesorder ×2 · customer ×4");
    expect(container.textContent).toContain("2 scripts");
    expect(container.textContent).toContain("1 diverged family");
    expect(container.textContent).toContain("cloned from a flow no longer in the account");
    expect(container.textContent).toContain("modified in Celigo 2 Sep 2026");

    expect(screen.getByText(deriveFlowSummary(makeDetail()))).toBeInTheDocument();
    expect(screen.getByText(/inherited from the clone source/)).toBeInTheDocument();
    expect(screen.getByText(AI_TEXT)).toBeInTheDocument();

    const link = screen.getByRole("link", { name: /Open in Celigo/ });
    expect(link).toHaveAttribute(
      "href",
      "https://integrator.io/integrations/600a502a-integration/flowBuilder/66738c3d-flow",
    );
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");

    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(window.location.href);
  });

  it("resolves 'cloned from {name}' when a sibling flow's celigo_id matches source_id", () => {
    mocks.detail.mockReturnValue(resolved(makeDetail({ source_id: "cel-flow-2" })));
    const { container } = wrap(<CeligoFlowPage />);
    expect(container.textContent).toContain("cloned from NS > Solidus - Shipping Confirmations v2");
  });

  it("withholds the cloned-from fact when the siblings fetch fails, instead of asserting the sibling is gone", () => {
    // Regression: the guard only withheld the fact while the siblings query
    // was PENDING. An errored siblings fetch collapsed to the same empty
    // array used for "no data yet", which then reads as a confirmed
    // negative — "cloned from a flow no longer in the account" — even
    // though the real sibling that resolves the name may exist and simply
    // failed to load. A failed request must render differently from both
    // "still loading" and "confirmed absent".
    mocks.detail.mockReturnValue(resolved(makeDetail({ source_id: "cel-flow-2" })));
    mocks.integrationFlows.mockReturnValue(errored());
    const { container } = wrap(<CeligoFlowPage />);
    expect(container.textContent).not.toContain("cloned from");
  });
});

describe("CeligoFlowPage — navigator", () => {
  it("has the panel group id, starts as a collapsed rail with one dot per sibling, expands on celigo:toggle-nav, and navigates on click", () => {
    const { container } = wrap(<CeligoFlowPage />);

    expect(container.querySelector("#celigo-flow-v1")).toBeInTheDocument();

    const rail = screen.getByTestId("celigo-nav-rail");
    const dots = Array.from(rail.querySelectorAll<HTMLElement>("span[data-state]"));
    expect(dots).toHaveLength(SIBLINGS.length);
    const currentDot = dots.find((d) => d.getAttribute("data-current") === "true");
    expect(currentDot).toBeDefined();
    expect(currentDot).toHaveAttribute("data-state", "on_time");
    expect(dots.filter((d) => d.getAttribute("data-current") === "true")).toHaveLength(1);

    fireEvent(window, new CustomEvent("celigo:toggle-nav"));

    expect(screen.queryByTestId("celigo-nav-rail")).not.toBeInTheDocument();
    const list = screen.getByTestId("celigo-nav-list");
    fireEvent.click(within(list).getByText("NS > Solidus - Shipping Confirmations v2"));
    expect(routeMocks.go.flow).toHaveBeenCalledWith("flow-2");
  });
});

describe("CeligoFlowPage — paused / empty states", () => {
  it("shows the exact paused banner and marks the canvas host data-paused", () => {
    mocks.detail.mockReturnValue(resolved(makeDetail({ disabled: true })));
    wrap(<CeligoFlowPage />);

    expect(
      screen.getByText("This flow is Off in Celigo — mirrored here, not changeable here."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("celigo-canvas-host")).toHaveAttribute("data-paused", "true");
  });

  it("shows the no-steps sentence in the canvas slot instead of mounting the canvas", () => {
    mocks.detail.mockReturnValue(resolved(makeDetail({ steps: [] })));
    wrap(<CeligoFlowPage />);

    expect(screen.getByText("No steps recorded for this flow in the last sync.")).toBeInTheDocument();
    expect(screen.queryByTestId("celigo-flow-canvas-stub")).not.toBeInTheDocument();
  });
});

describe("CeligoFlowPage — failed / unknown-id detail", () => {
  it("keeps the breadcrumb and shows a Retry-able error notice on a non-404 failure", () => {
    mocks.detail.mockReturnValue(errored());
    wrap(<CeligoFlowPage />);

    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
    expect(screen.getByText("My integrations")).toBeInTheDocument();
    expect(screen.getByText("Couldn't load this flow.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("shows the unknown-id sentence and a link back to the integration on a 404", () => {
    // The real api-client throws an Error whose message is the BACKEND's own
    // `detail` text (e.g. "Flow not found" — never containing the literal
    // "404"), with `.status` attached (`ApiError`, see api-client.test.ts).
    // Shaped that way here, not as a bare `{ status: 404 }` object, so this
    // test proves the page reads the real thrown-error shape, not a fixture
    // convenient to only itself.
    mocks.detail.mockReturnValue({
      ...errored(),
      error: Object.assign(new Error("Flow not found"), { status: 404 }),
    });
    wrap(<CeligoFlowPage />);

    expect(screen.getByText("My integrations")).toBeInTheDocument();
    expect(screen.getByText("This flow is not in the last sync.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Back to the integration" }));
    expect(routeMocks.go.integration).toHaveBeenCalledWith("int-1");
  });

  it("does not mistake a non-404 error for an unknown flow id just because its message contains '404'", () => {
    // Regression: is404() used to fall back to a `/\b404\b/` regex over the
    // error MESSAGE. A real failure whose status is NOT 404 but whose text
    // happens to contain that number (a gateway/proxy error, a port number,
    // anything) would incorrectly render the confident "not in the last
    // sync" claim instead of the generic failed-to-load state. Status alone
    // must decide this, never the message.
    mocks.detail.mockReturnValue({
      ...errored(),
      error: Object.assign(new Error("Gateway error 404: upstream timed out"), { status: 502 }),
    });
    wrap(<CeligoFlowPage />);

    expect(screen.queryByText("This flow is not in the last sync.")).not.toBeInTheDocument();
    expect(screen.getByText("Couldn't load this flow.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("CeligoFlowPage — keyboard", () => {
  it("Escape closes an open script drawer first, then clears the step on a second Escape", () => {
    routeMocks.stepId = "s5";
    routeMocks.scriptId = "script-x";
    const { rerender } = wrap(<CeligoFlowPage />);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(routeMocks.go.script).toHaveBeenCalledWith(null);
    expect(routeMocks.go.step).not.toHaveBeenCalled();

    routeMocks.scriptId = null;
    rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <CeligoFlowPage />
      </QueryClientProvider>,
    );

    fireEvent.keyDown(window, { key: "Escape" });
    expect(routeMocks.go.step).toHaveBeenCalledWith(null);

    expect(routeMocks.go.script.mock.invocationCallOrder[0]).toBeLessThan(
      routeMocks.go.step.mock.invocationCallOrder[0],
    );
  });
});

describe("CeligoFlowPage — script drawer", () => {
  // Task 17 -- the drawer is now always mounted (`CeligoScriptDrawer`'s own
  // `open={!!scriptId}` controls visibility). Regression: `useCeligoScript`
  // above is mocked to return a script WITH real content on every render of
  // this file's tests, precisely so a leak while `scriptId` is null (the
  // default here) would show up as this marker appearing in the document,
  // not merely as an absent dialog role.
  it("does not mount the drawer, and never leaks script content, when no scriptId is selected", () => {
    wrap(<CeligoFlowPage />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("UNIQUE_SCRIPT_MARKER_FOR_LEAK_TEST");
  });

  it("mounts the drawer as a dialog when route.scriptId is set", () => {
    routeMocks.scriptId = "script-x";
    wrap(<CeligoFlowPage />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(document.body.textContent).toContain("UNIQUE_SCRIPT_MARKER_FOR_LEAK_TEST");
  });
});

describe("CeligoFlowPage — inspector resting state", () => {
  it("renders the Overview (AI description + sync freshness) and hands the inspector slot step: null", () => {
    wrap(<CeligoFlowPage />);

    const overview = screen.getByTestId("celigo-overview");
    expect(within(overview).getByText(AI_TEXT)).toBeInTheDocument();
    expect(within(overview).getByText(/Synced 21 min ago/)).toBeInTheDocument();

    const inspector = screen.getByTestId("celigo-step-inspector-stub");
    expect(inspector).toHaveTextContent("no step selected");
  });
});
