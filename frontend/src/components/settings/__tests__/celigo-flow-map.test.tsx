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
  // Task 10 -- CeligoFlowMap now always mounts CeligoScriptViewerDialog, so
  // its hook is called on every render regardless of whether a script is
  // open (`enabled: false` while `scriptId` is null/undefined).
  script: vi.fn(),
}));

vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoIntegrations: () => mocks.integrations(),
  useCeligoAllFlows: () => mocks.allFlows(),
  useCeligoFlowDetail: (flowId: string | undefined) => mocks.flowDetail(flowId),
  useCeligoSyncStatus: () => mocks.syncStatus(),
  useCeligoScript: (scriptId: string | undefined) => mocks.script(scriptId),
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

/** A RESOLVED per-integration flows query, shaped like TanStack v5's result:
 * `status: "success"` with `isPending`/`isError` false. The component keys
 * off `isPending || isError` (== `status !== "success"`), never `isLoading`
 * -- see the "unresolved query" describe block for the live defect that
 * distinction exists for. */
function resolved(data: unknown) {
  return { data, status: "success", isPending: false, isLoading: false, isError: false, isSuccess: true, refetch: vi.fn() };
}

/** A query with no data yet: fetching, paused for retry, or disabled --
 * `status: "pending"`, `isLoading` deliberately false so a test cannot pass
 * on the old, wrong predicate. */
function pending() {
  return { data: undefined, status: "pending", isPending: true, isLoading: false, isError: false, isSuccess: false, refetch: vi.fn() };
}

function errored(refetch = vi.fn()) {
  return { data: undefined, status: "error", isPending: false, isLoading: false, isError: true, isSuccess: false, refetch };
}

function setLists(flows: unknown[][]) {
  mocks.allFlows.mockReturnValue(flows.map(resolved));
}

beforeEach(() => {
  mocks.integrations.mockReset();
  mocks.allFlows.mockReset();
  mocks.flowDetail.mockReset();
  mocks.flowDetail.mockReturnValue(pending());
  mocks.syncStatus.mockReset();
  mocks.syncStatus.mockReturnValue(pending());
  mocks.script.mockReset();
  mocks.script.mockReturnValue(pending());
});

describe("CeligoFlowMap — empty and loading states", () => {
  it("shows a graceful empty state when there are no synced integrations", () => {
    mocks.integrations.mockReturnValue(resolved([]));
    setLists([]);
    wrap(<CeligoFlowMap />);
    expect(screen.getByText(/no.*integrations.*synced/i)).toBeInTheDocument();
  });

  it("shows a loading state while integrations are still loading", () => {
    mocks.integrations.mockReturnValue(pending());
    setLists([]);
    wrap(<CeligoFlowMap />);
    expect(screen.queryByText(/no.*integrations.*synced/i)).not.toBeInTheDocument();
  });
});

describe("CeligoFlowMap — stats strip", () => {
  it("aggregates integration, flow, and open-error counts across every integration", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow, failingFlow, pausedFlow]]);
    mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: null }));
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
// LIVE DEFECT (Framework staging, 2026-09-01). 26 of 36 per-integration flow
// queries failed at the transport layer (a backend 500 with no CORS headers
// reads as "Failed to fetch") and sat in TanStack's `status: "pending"` /
// `fetchStatus: "paused"` state. `isLoading` is pending AND fetching, so it
// is FALSE for a paused query -- the old `isLoading || isError` predicate
// treated those 26 as resolved: the tree printed "0 flows" for integrations
// holding 23, and the stats strip summed the 10 that got through as if they
// were the whole account. The predicate is `status !== "success"`; anything
// else is a stand-in that drifts from it.
// ---------------------------------------------------------------------------

describe("CeligoFlowMap — an unresolved per-integration query never reads as an empty one", () => {
  it('renders a pending (paused, not fetching) flows query as loading, not "0 flows"', () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    mocks.allFlows.mockReturnValue([
      { data: undefined, status: "pending", isPending: true, isLoading: false, isError: false, isSuccess: false, refetch: vi.fn() },
    ]);
    mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: null }));
    wrap(<CeligoFlowMap />);

    expect(screen.queryByText(/0 flows/i)).not.toBeInTheDocument();
    expect(screen.getByText(/loading flows/i)).toBeInTheDocument();
    const stats = within(screen.getByTestId("celigo-stats-strip"))
      .getAllByTestId("celigo-stat-value")
      .map((el) => el.textContent);
    expect(stats[1]).toBe("—");
    expect(stats[2]).toBe("—");
  });

  // GATE ROUND 3 (major): the same shape survived one level up -- the
  // top-level integrations query and the sync-status query still gated on
  // `isLoading`. Every query in this file now goes through one `queryState`
  // mapping, so there is no per-call-site predicate left to get wrong.
  it("renders a pending (paused) integrations query as loading, never as 'no integrations synced'", () => {
    mocks.integrations.mockReturnValue({
      data: undefined,
      status: "pending",
      isPending: true,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    setLists([]);
    wrap(<CeligoFlowMap />);
    expect(screen.queryByText(/no.*integrations.*synced/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/flow map/i)).not.toBeInTheDocument();
  });

  it('renders a pending (paused) sync-status query as "—", never as "Never synced"', () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    mocks.syncStatus.mockReturnValue({
      data: undefined,
      status: "pending",
      isPending: true,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    wrap(<CeligoFlowMap />);
    const stats = within(screen.getByTestId("celigo-stats-strip"))
      .getAllByTestId("celigo-stat-value")
      .map((el) => el.textContent);
    expect(stats[3]).toBe("—");
  });
});

describe("CeligoFlowMap — schedule is Celigo's real shape", () => {
  it("renders a cron-string schedule verbatim (96 of 239 live flows carry one; none carry an object)", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[{ ...healthyFlow, schedule: "? 0 */6 * * *" }]]);
    wrap(<CeligoFlowMap />);
    expect(screen.getByText("? 0 */6 * * *")).toBeInTheDocument();
    expect(screen.queryByText(/custom schedule/i)).not.toBeInTheDocument();
  });

  it("renders a shape nobody has seen yet (the API relays JSON as-is) as a generic label, never a crash", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[{ ...healthyFlow, schedule: [{ type: "cron", expr: "? 0 */6 * * *" }] }]]);
    wrap(<CeligoFlowMap />);
    expect(screen.getByText(/custom schedule/i)).toBeInTheDocument();
  });

  it('renders a JSON `false` schedule as the generic label, not as "on demand" (gate round 3)', () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[{ ...healthyFlow, schedule: false }]]);
    wrap(<CeligoFlowMap />);
    expect(screen.getByText(/custom schedule/i)).toBeInTheDocument();
    expect(screen.queryByText(/on demand/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Fix round 1 -- optional addition: Task 8 shipped GET /celigo/sync-status
// for the mockup's "Last synced" stat this task originally had to drop.
// ---------------------------------------------------------------------------

describe("CeligoFlowMap — Last synced stat", () => {
  it("shows a relative time when a sync has completed", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    const fiveMinutesAgo = new Date(Date.now() - 5 * 60_000).toISOString();
    mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: fiveMinutesAgo }));
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/5 min ago/i)).toBeInTheDocument();
  });

  it("shows 'Never synced' when no sync has ever completed, not a misleading blank", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: null }));
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/never synced/i)).toBeInTheDocument();
  });
});

describe("CeligoFlowMap — deviation 1: signature count leads, raw count secondary", () => {
  it("shows root-cause (signature) count as the lead, raw error count secondary", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[failingFlow]]);
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/3 root causes/i)).toBeInTheDocument();
    expect(screen.getByText(/12 errors/i)).toBeInTheDocument();
  });

  it("shows a healthy pill for a flow with no open errors", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    wrap(<CeligoFlowMap />);
    expect(screen.getByText(/healthy/i)).toBeInTheDocument();
  });
});

describe("CeligoFlowMap — deviation 2: paused flows stay visible", () => {
  it("renders a disabled flow dimmed with a Paused pill, never filtered out of the list", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[pausedFlow]]);
    wrap(<CeligoFlowMap />);

    expect(screen.getByText("Legacy Return Sync")).toBeInTheDocument();
    expect(screen.getByText("Paused")).toBeInTheDocument();
  });
});

describe("CeligoFlowMap — tree", () => {
  it("shows the integration's flow count and a failing-count pill at lvl1", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow, failingFlow]]);
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/2 flows/i)).toBeInTheDocument();
    expect(screen.getByText(/1 failing/i)).toBeInTheDocument();
  });

  it("shows each flow's schedule in monospace at lvl2", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    wrap(<CeligoFlowMap />);
    expect(screen.getByText(/every 15 minutes/i)).toBeInTheDocument();
  });

  it('formats a flow with no schedule as "on demand"', () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[failingFlow]]); // schedule: null
    wrap(<CeligoFlowMap />);
    expect(screen.getByText(/on demand/i)).toBeInTheDocument();
  });

  it("expanding a flow row shows its steps as Source / Destination", async () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockImplementation((flowId: string | undefined) =>
      flowId === "flow-healthy"
        ? {
            data: { id: "flow-healthy", steps: [generatorStep, processorStep], unassigned_attachments: [] },
            isLoading: false,
            isPending: false,
            isError: false,
          }
        : pending(),
    );
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /expand.*inventory sync|inventory sync.*expand/i }));

    await waitFor(() => expect(screen.getByText(/source/i)).toBeInTheDocument());
    expect(screen.getByText(/destination/i)).toBeInTheDocument();
    expect(screen.getByText("NetSuiteExport")).toBeInTheDocument();
    expect(screen.getByText("NetSuiteDistributedImport")).toBeInTheDocument();
  });

  it("shows an amber script-count pill only on a step that has attachments", async () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockReturnValue({
      data: { id: "flow-healthy", steps: [generatorStep, processorStep], unassigned_attachments: [] },
      isLoading: false,
      isPending: false,
      isError: false,
    });
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /expand.*inventory sync|inventory sync.*expand/i }));

    await waitFor(() => expect(screen.getByText(/1 script/i)).toBeInTheDocument());
  });
});

describe("CeligoFlowMap — flow detail (screen 03)", () => {
  it("clicking a flow name opens its detail with a source/destination graph and field mapping", async () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
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
            isPending: false,
            isError: false,
          }
        : pending(),
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
    mocks.integrations.mockReturnValue(errored());
    setLists([]);
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/couldn.?t load/i)).toBeInTheDocument();
    expect(screen.queryByText(/no.*integrations.*synced/i)).not.toBeInTheDocument();
  });

  it("a failed integrations query offers a retry that calls refetch", () => {
    const refetch = vi.fn();
    mocks.integrations.mockReturnValue(errored(refetch));
    setLists([]);
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });

  it("a failed per-integration flows query marks that integration's card as errored, not silently 0 flows", () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    mocks.allFlows.mockReturnValue([
      errored(),
    ]);
    wrap(<CeligoFlowMap />);

    expect(screen.getByText(/couldn.?t load.*flows/i)).toBeInTheDocument();
    expect(screen.queryByText(/0 flows/i)).not.toBeInTheDocument();
  });

  it("expanding a flow whose step-detail query failed shows an error, not silence", async () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockImplementation((flowId: string | undefined) =>
      flowId === "flow-healthy"
        ? errored()
        : pending(),
    );
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /expand.*inventory sync|inventory sync.*expand/i }));

    await waitFor(() => expect(screen.getByText(/couldn.?t load.*steps/i)).toBeInTheDocument());
  });

  it("a failed per-integration flows query marks the stats strip, never a falsely-confident 0", () => {
    // WHOLE-BRANCH REVIEW FINDING 12 (2026-08-27): `flowQueries[index]?.data
    // ?? []` made a failed per-integration query contribute ZERO to both
    // "Flows" and "Open errors", with no marker distinguishing that from a
    // genuinely healthy zero -- unlike "Last synced" five lines below it in
    // the source, which already applies the "—" + unmarked-tone pattern for
    // exactly this reason. Two integrations: one healthy with real flows,
    // one whose flows query failed -- the strip must not silently drop the
    // failed one's contribution and present the healthy-only total as
    // complete.
    mocks.integrations.mockReturnValue({
      data: [integration, { ...integration, id: "int-2", celigo_id: "c-int-2", name: "Other ERP" }],
      isLoading: false,
      isPending: false,
      isError: false,
    });
    mocks.allFlows.mockReturnValue([
      resolved([healthyFlow, failingFlow]),
      errored(),
    ]);
    mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: null }));
    wrap(<CeligoFlowMap />);

    const stripe = within(screen.getByTestId("celigo-stats-strip"));
    const stats = stripe.getAllByTestId("celigo-stat-value").map((el) => el.textContent);
    // Integrations count (a real, always-known list) is untouched; Flows
    // and Open errors must show the same "unresolved" marker "Last synced"
    // uses, NOT "2" flows / "12" open errors, which would silently omit
    // whatever the failed integration's real flows/errors are.
    expect(stats[0]).toBe("2"); // Integrations
    expect(stats[1]).toBe("—"); // Flows -- NOT "2"
    expect(stats[2]).toBe("—"); // Open errors -- NOT "12"
  });

  it("the flow detail dialog shows an error instead of spinning forever when its query fails", async () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockImplementation((flowId: string | undefined) =>
      flowId === "flow-healthy"
        ? errored()
        : pending(),
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
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockImplementation((flowId: string | undefined) =>
      flowId === "flow-healthy"
        ? {
            data: { id: "flow-healthy", name: "Inventory Sync", schedule: null, steps: [], unassigned_attachments: [] },
            isLoading: false,
            isPending: false,
            isError: false,
          }
        : pending(),
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
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockReturnValue({
      data: {
        id: "flow-healthy",
        steps: [{ ...generatorStep, adaptor_type: "" }],
        unassigned_attachments: [],
      },
      isLoading: false,
      isPending: false,
      isError: false,
    });
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /expand.*inventory sync|inventory sync.*expand/i }));

    await waitFor(() => expect(screen.getByText(/unknown adaptor/i)).toBeInTheDocument());
  });

  it("renders 'Unknown adaptor' for a step whose adaptor_type is an empty string, in the screen 03 graph node", async () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
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
            isPending: false,
            isError: false,
          }
        : pending(),
    );
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: "Inventory Sync" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/unknown adaptor/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Task 10 -- a step's script chip opens the script viewer for that
// attachment's script_id.
// ---------------------------------------------------------------------------

describe("CeligoFlowMap — script viewer wiring (Task 10)", () => {
  it("clicking a step's script chip opens the script viewer for that attachment's script", async () => {
    mocks.integrations.mockReturnValue(resolved([integration]));
    setLists([[healthyFlow]]);
    mocks.flowDetail.mockReturnValue({
      data: { id: "flow-healthy", steps: [generatorStep, processorStep], unassigned_attachments: [] },
      isLoading: false,
      isPending: false,
      isError: false,
    });
    wrap(<CeligoFlowMap />);

    fireEvent.click(screen.getByRole("button", { name: /expand.*inventory sync|inventory sync.*expand/i }));
    await waitFor(() => expect(screen.getByText(/1 script/i)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /open attached script/i }));

    expect(mocks.script).toHaveBeenCalledWith("scr-1");
  });
});
