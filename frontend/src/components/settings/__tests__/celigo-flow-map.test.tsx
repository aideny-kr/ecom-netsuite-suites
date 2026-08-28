import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

// Task 9 — flow map (screen 02) + flow detail (screen 03). Mocks
// use-celigo-flows entirely, matching celigo-connector-card.test.tsx's
// established pattern of mocking the hooks module rather than apiClient.

const mocks = vi.hoisted(() => ({
  integrations: vi.fn(),
  allFlows: vi.fn(),
  flowDetail: vi.fn(),
  syncStatus: vi.fn(),
}));

vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoIntegrations: () => mocks.integrations(),
  useCeligoAllFlows: () => mocks.allFlows(),
  useCeligoFlowDetail: (flowId: string | undefined) => mocks.flowDetail(flowId),
  useCeligoSyncStatus: () => mocks.syncStatus(),
}));

import { CeligoFlowMap } from "../celigo-flow-map";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const integration = {
  id: "int-1",
  celigo_id: "c-int-1",
  name: "ACME ERP",
  sandbox: false,
  mode: "settings",
  description: null,
  celigo_last_modified: null,
};

const healthyFlow = {
  id: "flow-healthy",
  celigo_id: "c-flow-healthy",
  name: "Inventory Sync",
  disabled: false,
  schedule: { type: "everyN", unit: "minutes", value: 15 },
  timezone: "UTC",
  last_executed_at: null,
  error_count: 0,
  signature_count: 0,
};

const failingFlow = {
  id: "flow-failing",
  celigo_id: "c-flow-failing",
  name: "Sales Order Sync",
  disabled: false,
  schedule: null,
  timezone: "UTC",
  last_executed_at: null,
  error_count: 12,
  signature_count: 3,
};

const pausedFlow = {
  id: "flow-paused",
  celigo_id: "c-flow-paused",
  name: "Legacy Return Sync",
  disabled: true,
  schedule: { type: "everyN", unit: "hours", value: 1 },
  timezone: "UTC",
  last_executed_at: null,
  error_count: 0,
  signature_count: 0,
};

const generatorStep = {
  id: "step-source",
  celigo_id: "exp-1",
  role: "generator",
  router_id: null,
  branch_id: null,
  branch_key: "$root",
  sequence: 0,
  adaptor_type: "NetSuiteExport",
  connection_celigo_id: "conn-1",
  filter_json: null,
  mapping_json: null,
  proceed_on_failure: null,
  skip_retries: null,
  attachments: [],
};

const processorStep = {
  id: "step-dest",
  celigo_id: "imp-1",
  role: "processor",
  router_id: null,
  branch_id: null,
  branch_key: "$root",
  sequence: 1,
  adaptor_type: "NetSuiteDistributedImport",
  connection_celigo_id: "conn-2",
  filter_json: { type: "expression", expression: { rules: ["status", "=", "open"] } },
  mapping_json: { fields: [{ extract: "customer.id", generate: "entity" }] },
  proceed_on_failure: null,
  skip_retries: null,
  attachments: [
    {
      id: "att-1",
      flow_id: "flow-failing",
      flow_step_id: "step-dest",
      script_id: "scr-1",
      script_celigo_id: "scr-1",
      function_name: "transform",
      json_path: "pageProcessors[0].transform.script",
      site_type: "transform",
    },
  ],
};

function setLists(flows: unknown[][]) {
  mocks.allFlows.mockReturnValue(flows.map((data) => ({ data, isLoading: false, isSuccess: true })));
}

beforeEach(() => {
  mocks.integrations.mockReset();
  mocks.allFlows.mockReset();
  mocks.flowDetail.mockReset();
  mocks.flowDetail.mockReturnValue({ data: undefined, isLoading: false });
  mocks.syncStatus.mockReset();
  mocks.syncStatus.mockReturnValue({ data: undefined, isLoading: true });
});

describe("CeligoFlowMap — empty and loading states", () => {
  it("shows a graceful empty state when there are no synced integrations", () => {
    mocks.integrations.mockReturnValue({ data: [], isLoading: false });
    setLists([]);
    wrap(<CeligoFlowMap />);
    expect(screen.getByText(/no.*integrations.*synced/i)).toBeInTheDocument();
  });

  it("shows a loading state while integrations are still loading", () => {
    mocks.integrations.mockReturnValue({ data: undefined, isLoading: true });
    setLists([]);
    wrap(<CeligoFlowMap />);
    expect(screen.queryByText(/no.*integrations.*synced/i)).not.toBeInTheDocument();
  });
});

describe("CeligoFlowMap — stats strip", () => {
  it("aggregates integration, flow, and open-error counts across every integration", () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow, failingFlow, pausedFlow]]);
    mocks.syncStatus.mockReturnValue({ data: { last_synced_at: null }, isLoading: false });
    wrap(<CeligoFlowMap />);

    const stripe = within(screen.getByTestId("celigo-stats-strip"));
    expect(stripe.getByText("Integrations")).toBeInTheDocument();
    expect(stripe.getByText("Flows")).toBeInTheDocument();
    expect(stripe.getByText("Open errors")).toBeInTheDocument();
    expect(stripe.getByText("Last synced")).toBeInTheDocument();
    // 1 integration, 3 flows, 12 open errors (only failingFlow has any), never synced
    const stats = stripe.getAllByTestId("celigo-stat-value").map((el) => el.textContent);
    expect(stats).toEqual(["1", "3", "12", "Never synced"]);
  });
});

// ---------------------------------------------------------------------------
// Fix round 1 -- optional addition: Task 8 shipped GET /celigo/sync-status
// for the mockup's "Last synced" stat this task originally had to drop.
// ---------------------------------------------------------------------------

describe("CeligoFlowMap — Last synced stat", () => {
  it("shows a relative time when a sync has completed", () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    const fiveMinutesAgo = new Date(Date.now() - 5 * 60_000).toISOString();
    mocks.syncStatus.mockReturnValue({ data: { last_synced_at: fiveMinutesAgo }, isLoading: false });
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/5 min ago/i)).toBeInTheDocument();
  });

  it("shows 'Never synced' when no sync has ever completed, not a misleading blank", () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    mocks.syncStatus.mockReturnValue({ data: { last_synced_at: null }, isLoading: false });
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/never synced/i)).toBeInTheDocument();
  });
});

describe("CeligoFlowMap — deviation 1: signature count leads, raw count secondary", () => {
  it("shows root-cause (signature) count as the lead, raw error count secondary", () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[failingFlow]]);
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/3 root causes/i)).toBeInTheDocument();
    expect(screen.getByText(/12 errors/i)).toBeInTheDocument();
  });

  it("shows a healthy pill for a flow with no open errors", () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    wrap(<CeligoFlowMap />);
    expect(screen.getByText(/healthy/i)).toBeInTheDocument();
  });
});

describe("CeligoFlowMap — deviation 2: paused flows stay visible", () => {
  it("renders a disabled flow dimmed with a Paused pill, never filtered out of the list", () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[pausedFlow]]);
    wrap(<CeligoFlowMap />);

    expect(screen.getByText("Legacy Return Sync")).toBeInTheDocument();
    expect(screen.getByText("Paused")).toBeInTheDocument();
  });
});

describe("CeligoFlowMap — tree", () => {
  it("shows the integration's flow count and a failing-count pill at lvl1", () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow, failingFlow]]);
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/2 flows/i)).toBeInTheDocument();
    expect(screen.getByText(/1 failing/i)).toBeInTheDocument();
  });

  it("shows each flow's schedule in monospace at lvl2", () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    wrap(<CeligoFlowMap />);
    expect(screen.getByText(/every 15 minutes/i)).toBeInTheDocument();
  });

  it('formats a flow with no schedule as "on demand"', () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[failingFlow]]); // schedule: null
    wrap(<CeligoFlowMap />);
    expect(screen.getByText(/on demand/i)).toBeInTheDocument();
  });

  it("expanding a flow row shows its steps as Source / Destination", async () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockImplementation((flowId: string | undefined) =>
      flowId === "flow-healthy"
        ? {
            data: { id: "flow-healthy", steps: [generatorStep, processorStep], unassigned_attachments: [] },
            isLoading: false,
          }
        : { data: undefined, isLoading: false },
    );
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /expand.*inventory sync|inventory sync.*expand/i }));

    await waitFor(() => expect(screen.getByText(/source/i)).toBeInTheDocument());
    expect(screen.getByText(/destination/i)).toBeInTheDocument();
    expect(screen.getByText("NetSuiteExport")).toBeInTheDocument();
    expect(screen.getByText("NetSuiteDistributedImport")).toBeInTheDocument();
  });

  it("shows an amber script-count pill only on a step that has attachments", async () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockReturnValue({
      data: { id: "flow-healthy", steps: [generatorStep, processorStep], unassigned_attachments: [] },
      isLoading: false,
    });
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /expand.*inventory sync|inventory sync.*expand/i }));

    await waitFor(() => expect(screen.getByText(/1 script/i)).toBeInTheDocument());
  });
});

describe("CeligoFlowMap — flow detail (screen 03)", () => {
  it("clicking a flow name opens its detail with a source/destination graph and field mapping", async () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockImplementation((flowId: string | undefined) =>
      flowId === "flow-healthy"
        ? {
            data: {
              id: "flow-healthy",
              name: "Inventory Sync",
              schedule: { type: "everyN", unit: "minutes", value: 15 },
              steps: [generatorStep, processorStep],
              unassigned_attachments: [],
            },
            isLoading: false,
          }
        : { data: undefined, isLoading: false },
    );
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: "Inventory Sync" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/source/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/destination/i)).toBeInTheDocument();
    // Field mapping table renders the CONFIRMED {fields:[{extract,generate}]} shape.
    expect(within(dialog).getByText("entity")).toBeInTheDocument();
    expect(within(dialog).getByText("customer.id")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Fix round 1 -- Important: a query error must never render as an empty
// state. Reviewer's exact concern: `useCeligoIntegrations` failing renders
// the SAME copy as "genuinely no integrations", telling the operator to
// reconnect a connection that's actually fine.
// ---------------------------------------------------------------------------

describe("CeligoFlowMap — error states are distinct from empty states (fix round 1)", () => {
  it("a failed integrations query shows an error, never the 'no integrations synced' copy", () => {
    mocks.integrations.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch: vi.fn(),
    });
    setLists([]);
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/couldn.?t load/i)).toBeInTheDocument();
    expect(screen.queryByText(/no.*integrations.*synced/i)).not.toBeInTheDocument();
  });

  it("a failed integrations query offers a retry that calls refetch", () => {
    const refetch = vi.fn();
    mocks.integrations.mockReturnValue({ data: undefined, isLoading: false, isError: true, refetch });
    setLists([]);
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });

  it("a failed per-integration flows query marks that integration's card as errored, not silently 0 flows", () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    mocks.allFlows.mockReturnValue([
      { data: undefined, isLoading: false, isError: true, refetch: vi.fn() },
    ]);
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/couldn.?t load.*flows/i)).toBeInTheDocument();
    expect(screen.queryByText(/0 flows/i)).not.toBeInTheDocument();
  });

  it("expanding a flow whose step-detail query failed shows an error, not silence", async () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockImplementation((flowId: string | undefined) =>
      flowId === "flow-healthy"
        ? { data: undefined, isLoading: false, isError: true, refetch: vi.fn() }
        : { data: undefined, isLoading: false },
    );
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /expand.*inventory sync|inventory sync.*expand/i }));

    await waitFor(() => expect(screen.getByText(/couldn.?t load.*steps/i)).toBeInTheDocument());
  });

  it("the flow detail dialog shows an error instead of spinning forever when its query fails", async () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockImplementation((flowId: string | undefined) =>
      flowId === "flow-healthy"
        ? { data: undefined, isLoading: false, isError: true, refetch: vi.fn() }
        : { data: undefined, isLoading: false },
    );
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: "Inventory Sync" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/couldn.?t load/i)).toBeInTheDocument();
    expect(within(dialog).queryByText(/loading flow/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Fix round 1 -- Minor 2: the graph strip needs a "no steps" fallback,
// matching the Filter/Field-mapping panels right below it.
// ---------------------------------------------------------------------------

describe("CeligoFlowMap — flow detail with zero steps (fix round 1)", () => {
  it("shows a graph-strip empty state for a flow with no steps, matching the filter/mapping fallback style", async () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockImplementation((flowId: string | undefined) =>
      flowId === "flow-healthy"
        ? {
            data: { id: "flow-healthy", name: "Inventory Sync", schedule: null, steps: [], unassigned_attachments: [] },
            isLoading: false,
          }
        : { data: undefined, isLoading: false },
    );
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: "Inventory Sync" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/no steps configured/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Fix round 1 -- Minor 3: `adaptor_type: ""` must degrade the same as null
// (the exact bug shape that shipped earlier this session for account_name).
// ---------------------------------------------------------------------------

describe("CeligoFlowMap — empty-string adaptor_type treated as missing (fix round 1)", () => {
  it("renders 'Unknown adaptor' for a step whose adaptor_type is an empty string, at lvl3", async () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockReturnValue({
      data: {
        id: "flow-healthy",
        steps: [{ ...generatorStep, adaptor_type: "" }],
        unassigned_attachments: [],
      },
      isLoading: false,
    });
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /expand.*inventory sync|inventory sync.*expand/i }));

    await waitFor(() => expect(screen.getByText(/unknown adaptor/i)).toBeInTheDocument());
  });

  it("renders 'Unknown adaptor' for a step whose adaptor_type is an empty string, in the screen 03 graph node", async () => {
    mocks.integrations.mockReturnValue({ data: [integration], isLoading: false });
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockImplementation((flowId: string | undefined) =>
      flowId === "flow-healthy"
        ? {
            data: {
              id: "flow-healthy",
              name: "Inventory Sync",
              schedule: null,
              steps: [{ ...generatorStep, adaptor_type: "" }],
              unassigned_attachments: [],
            },
            isLoading: false,
          }
        : { data: undefined, isLoading: false },
    );
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: "Inventory Sync" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/unknown adaptor/i)).toBeInTheDocument();
  });
});
