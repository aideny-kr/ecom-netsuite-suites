import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import type { CeligoIntegration, CeligoFlowSchedule } from "@/hooks/use-celigo-flows";
import { resolved, pending, errored } from "./query-fixtures";

// Task 10 — "My integrations" (mockup screen 1): tiles, list view, filters,
// attention-first sort. Mocks the hooks module (celigo-flow-map.test.tsx's
// established pattern) and the route module (mirrors Task 9's own
// celigo-route.test.tsx nav mock, one level up: the route hook itself is
// stubbed rather than next/navigation, since nothing here exercises
// `readCeligoRoute`'s URL decoding).

const mocks = vi.hoisted(() => ({
  integrations: vi.fn(),
  syncStatus: vi.fn(),
}));

vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoIntegrations: () => mocks.integrations(),
  useCeligoSyncStatus: () => mocks.syncStatus(),
}));

const routeMocks = vi.hoisted(() => ({
  view: "tiles" as "tiles" | "list",
  go: {
    files: vi.fn(),
    integrations: vi.fn(),
    integration: vi.fn(),
    tab: vi.fn(),
    view: vi.fn(),
    flow: vi.fn(),
    step: vi.fn(),
    script: vi.fn(),
  },
}));

vi.mock("../celigo-route", () => ({
  useCeligoRoute: () => ({
    surface: "celigo" as const,
    view: routeMocks.view,
    integrationId: null,
    tab: "flows" as const,
    flowId: null,
    stepId: null,
    scriptId: null,
    go: routeMocks.go,
  }),
}));

import { CeligoIntegrationsPage, integrationAttention, sortIntegrations } from "../celigo-integrations-page";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// Sync completed 18:12 UTC; a flow last ran 17:51 UTC → "21 min ago" for
// both the "checked" timestamp and (in the flow's own schedule) the stall
// check's reference clock. Matches the mockup's own numbers exactly.
const NOW = new Date("2026-09-02T18:12:00.000Z");
const SYNCED_AT = "2026-09-02T17:51:00.000Z"; // 21 min before NOW

function makeIntegration(overrides: Partial<CeligoIntegration> = {}): CeligoIntegration {
  return {
    id: "int-1",
    celigo_id: "c-int-1",
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
    last_run_at: "2026-09-02T18:06:00.000Z",
    writes: [
      { record_type: "salesorder", count: 19 },
      { record_type: "customer", count: 9 },
      { record_type: "itemfulfillment", count: 2 },
      { record_type: "customerdeposit", count: 2 },
    ],
    adaptor_families: ["NetSuite", "HTTP"],
    flow_schedules: [],
    ...overrides,
  };
}

function flowSchedule(overrides: Partial<CeligoFlowSchedule> = {}): CeligoFlowSchedule {
  return {
    id: "fs-1",
    name: "Some Flow",
    disabled: false,
    schedule: "? 0,15,30,45 * ? * *", // every 15 min, all hours
    last_executed_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  mocks.integrations.mockReset();
  mocks.syncStatus.mockReset();
  mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: SYNCED_AT }));
  routeMocks.view = "tiles";
  routeMocks.go.files.mockReset();
  routeMocks.go.integrations.mockReset();
  routeMocks.go.integration.mockReset();
  routeMocks.go.tab.mockReset();
  routeMocks.go.view.mockReset();
  routeMocks.go.flow.mockReset();
  routeMocks.go.step.mockReset();
  routeMocks.go.script.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("integrationAttention", () => {
  it("counts stalled flow_schedules against the sync timestamp", () => {
    const stalled = flowSchedule({ last_executed_at: "2026-09-02T14:51:00.000Z" }); // 3h before sync
    const onTime = flowSchedule({ id: "fs-2", last_executed_at: "2026-09-02T17:50:00.000Z" });
    const i = makeIntegration({ flow_schedules: [stalled, onTime] });
    expect(integrationAttention(i, SYNCED_AT).stalledCount).toBe(1);
  });

  it("flags all-paused only when every flow is paused", () => {
    const allPaused = makeIntegration({ flow_count: 3, paused_count: 3 });
    const notAllPaused = makeIntegration({ flow_count: 3, paused_count: 2 });
    const empty = makeIntegration({ flow_count: 0, paused_count: 0 });
    expect(integrationAttention(allPaused, SYNCED_AT).allPaused).toBe(true);
    expect(integrationAttention(notAllPaused, SYNCED_AT).allPaused).toBe(false);
    expect(integrationAttention(empty, SYNCED_AT).allPaused).toBe(false);
  });

  it("flags on-demand-only when nothing is scheduled but something runs on demand", () => {
    const onDemandOnly = makeIntegration({ scheduled_count: 0, on_demand_count: 4 });
    const scheduled = makeIntegration({ scheduled_count: 1, on_demand_count: 4 });
    expect(integrationAttention(onDemandOnly, SYNCED_AT).onDemandOnly).toBe(true);
    expect(integrationAttention(scheduled, SYNCED_AT).onDemandOnly).toBe(false);
  });
});

describe("sortIntegrations", () => {
  it("orders by error_count desc, then stalledCount desc, then flow_count desc, then name", () => {
    const stalledSmall = makeIntegration({
      id: "int-stalled",
      name: "Stalled Co",
      flow_count: 5,
      flow_schedules: [flowSchedule({ last_executed_at: "2026-09-02T14:51:00.000Z" })],
    });
    const quietBig = makeIntegration({ id: "int-quiet", name: "Quiet Big Co", flow_count: 50 });
    const errored = makeIntegration({ id: "int-err", name: "Errored Co", flow_count: 1, error_count: 3 });
    const sorted = sortIntegrations([quietBig, stalledSmall, errored], SYNCED_AT);
    expect(sorted.map((i) => i.name)).toEqual(["Errored Co", "Stalled Co", "Quiet Big Co"]);
  });
});

describe("CeligoIntegrationsPage", () => {
  it("case 1: renders tile facts, writes, error pill and header stats for resolved data", () => {
    const a = makeIntegration();
    const b = makeIntegration({
      id: "int-2",
      name: "Backfills",
      flow_count: 7,
      scheduled_count: 2,
      on_demand_count: 4,
      paused_count: 1,
      step_count: 20,
      script_count: 5,
      writes: [{ record_type: "itemfulfillment", count: 3 }],
      adaptor_families: ["HTTP"],
    });
    mocks.integrations.mockReturnValue(resolved([a, b]));

    wrap(<CeligoIntegrationsPage />);

    const tileA = screen.getByText("Solidus + NetSuite").closest("button") as HTMLElement;
    expect(within(tileA).getByText("20 flows · 9 scheduled · 6 on demand · 5 paused · 94 steps")).toBeInTheDocument();
    expect(within(tileA).getByText("salesorder ×19")).toBeInTheDocument();
    expect(within(tileA).getByText(/0 open errors/)).toBeInTheDocument();
    expect(within(tileA).getByText(/checked 21 min ago/)).toBeInTheDocument();
    expect(within(tileA).getByText("NS")).toBeInTheDocument();
    expect(within(tileA).getByText("HTTP")).toBeInTheDocument();

    const header = screen.getByText(/integrations ·/);
    expect(header.textContent).toContain("2 integrations");
    expect(header.textContent).toContain("27 flows");
    expect(header.textContent).toContain("production only");
  });

  it("case 2: sorts a stalled integration ahead of a bigger quiet one and shows its stalled pill", () => {
    const stalled = makeIntegration({
      id: "int-stalled",
      name: "Stalled Co",
      flow_count: 5,
      scheduled_count: 1,
      on_demand_count: 4,
      paused_count: 0,
      step_count: 10,
      writes: [],
      adaptor_families: ["NetSuite"],
      flow_schedules: [flowSchedule({ last_executed_at: "2026-09-02T14:51:00.000Z" })],
    });
    const quietBig = makeIntegration({
      id: "int-quiet",
      name: "Quiet Big Co",
      flow_count: 50,
      scheduled_count: 50,
      on_demand_count: 0,
      paused_count: 0,
      step_count: 200,
      writes: [],
      adaptor_families: ["NetSuite"],
      flow_schedules: [],
    });
    mocks.integrations.mockReturnValue(resolved([quietBig, stalled]));

    wrap(<CeligoIntegrationsPage />);

    const headings = screen.getAllByRole("heading", { level: 4 }).map((h) => h.textContent);
    expect(headings.indexOf("Stalled Co")).toBeLessThan(headings.indexOf("Quiet Big Co"));
    expect(screen.getByText("stalled? 1 flow")).toBeInTheDocument();
  });

  it("case 3: dims an all-paused integration and labels an on-demand-only one", () => {
    const allPaused = makeIntegration({
      id: "int-paused",
      name: "All Paused Co",
      flow_count: 3,
      scheduled_count: 0,
      on_demand_count: 0,
      paused_count: 3,
      step_count: 6,
      writes: [],
      adaptor_families: ["NetSuite"],
    });
    const onDemandOnly = makeIntegration({
      id: "int-ondemand",
      name: "On Demand Co",
      flow_count: 4,
      scheduled_count: 0,
      on_demand_count: 4,
      paused_count: 0,
      step_count: 8,
      writes: [],
      adaptor_families: ["HTTP"],
    });
    mocks.integrations.mockReturnValue(resolved([allPaused, onDemandOnly]));

    wrap(<CeligoIntegrationsPage />);

    const pausedTile = screen.getByText("All Paused Co").closest("button");
    expect(pausedTile).toHaveAttribute("data-state", "all-paused");
    expect(within(pausedTile as HTMLElement).getByText("all paused")).toBeInTheDocument();

    const onDemandTile = screen.getByText("On Demand Co").closest("button");
    expect(onDemandTile).not.toHaveAttribute("data-state", "all-paused");
    expect(within(onDemandTile as HTMLElement).getByText("on demand only")).toBeInTheDocument();
  });

  it("writes overflow only claims 'custom records' when every hidden type actually is one", () => {
    const genuinelyCustom = makeIntegration({
      id: "int-custom",
      name: "Custom Overflow Co",
      writes: [
        { record_type: "salesorder", count: 5 },
        { record_type: "customer", count: 4 },
        { record_type: "itemfulfillment", count: 3 },
        { record_type: "purchaseorder", count: 2 },
        { record_type: "customrecord_po_ack", count: 1 },
        { record_type: "customrecord_x", count: 1 },
      ],
    });
    const notActuallyCustom = makeIntegration({
      id: "int-standard",
      name: "Standard Overflow Co",
      writes: [
        { record_type: "salesorder", count: 5 },
        { record_type: "customer", count: 4 },
        { record_type: "itemfulfillment", count: 3 },
        { record_type: "purchaseorder", count: 2 },
        { record_type: "invoice", count: 1 }, // a 5th STANDARD type, not customrecord_*
      ],
    });
    mocks.integrations.mockReturnValue(resolved([genuinelyCustom, notActuallyCustom]));

    wrap(<CeligoIntegrationsPage />);

    const customTile = screen.getByText("Custom Overflow Co").closest("button") as HTMLElement;
    expect(within(customTile).getByText("+2 custom records")).toBeInTheDocument();

    const standardTile = screen.getByText("Standard Overflow Co").closest("button") as HTMLElement;
    expect(within(standardTile).queryByText(/custom record/)).toBeNull();
    expect(within(standardTile).getByText("+1 more")).toBeInTheDocument();
  });

  it("case 4: Stalled and Open errors filters each isolate their tile", () => {
    const stalledOnly = makeIntegration({
      id: "int-s",
      name: "Stalled Only Co",
      error_count: 0,
      flow_schedules: [flowSchedule({ last_executed_at: "2026-09-02T14:51:00.000Z" })],
    });
    const erroredOnly = makeIntegration({ id: "int-e", name: "Errored Co", error_count: 3, flow_schedules: [] });
    const plain = makeIntegration({ id: "int-p", name: "Plain Co", error_count: 0, flow_schedules: [] });
    mocks.integrations.mockReturnValue(resolved([stalledOnly, erroredOnly, plain]));

    wrap(<CeligoIntegrationsPage />);

    fireEvent.click(screen.getByRole("button", { name: /^Stalled/ }));
    expect(screen.getByText("Stalled Only Co")).toBeInTheDocument();
    expect(screen.queryByText("Errored Co")).toBeNull();
    expect(screen.queryByText("Plain Co")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^Open errors/ }));
    expect(screen.getByText("Errored Co")).toBeInTheDocument();
    expect(screen.queryByText("Stalled Only Co")).toBeNull();
    expect(screen.queryByText("Plain Co")).toBeNull();
  });

  it("case 5: the list toggle switches view in place, and list view renders the same counts", () => {
    // Gate fix wave, item 7: flipping tiles/list is a selection change on the
    // page already on screen, so it replaces rather than pushing a history
    // entry a reader would then have to Back out of one toggle at a time.
    mocks.integrations.mockReturnValue(resolved([makeIntegration()]));
    wrap(<CeligoIntegrationsPage />);
    fireEvent.click(screen.getByRole("button", { name: /list view/i }));
    expect(routeMocks.go.view).toHaveBeenCalledWith("list");
    expect(routeMocks.go.integrations).not.toHaveBeenCalled();
  });

  it("case 5b: with view=list the table renders rows with the same counts", () => {
    routeMocks.view = "list";
    mocks.integrations.mockReturnValue(resolved([makeIntegration()]));
    wrap(<CeligoIntegrationsPage />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("20 flows · 9 scheduled · 6 on demand · 5 paused · 94 steps")).toBeInTheDocument();
  });

  it("case 6a: a pending integrations query renders a skeleton, never 'No integrations'", () => {
    mocks.integrations.mockReturnValue(pending());
    const { container } = wrap(<CeligoIntegrationsPage />);
    expect(screen.queryByText(/no integrations/i)).toBeNull();
    expect(screen.queryByText(/no flows synced/i)).toBeNull();
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it("case 6b: an errored integrations query shows ErrorNotice with a Retry that calls refetch", () => {
    const refetch = vi.fn();
    mocks.integrations.mockReturnValue(errored(refetch));
    wrap(<CeligoIntegrationsPage />);
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });

  it("case 6c: resolved empty + never-synced shows the empty-state copy and '—' counts", () => {
    mocks.integrations.mockReturnValue(resolved([]));
    mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: null }));
    wrap(<CeligoIntegrationsPage />);
    expect(
      screen.getByText("No flows synced yet — run a sync from the connector card in Settings."),
    ).toBeInTheDocument();
    const header = screen.getByText(/— integrations/);
    expect(header.textContent).toContain("—");
  });

  it("case 7: clicking a tile calls go.integration(id)", () => {
    const a = makeIntegration({ id: "int-click-me" });
    mocks.integrations.mockReturnValue(resolved([a]));
    wrap(<CeligoIntegrationsPage />);
    fireEvent.click(screen.getByText("Solidus + NetSuite"));
    expect(routeMocks.go.integration).toHaveBeenCalledWith("int-click-me");
  });

  // Fix round 1, finding 1: syncStatusQuery's pending/error states were
  // never distinguished from "resolved with no sync yet" — a race between
  // the two independent queries (integrations resolves first) must never
  // collapse into the confident "never synced" copy, an under-reported
  // stalled count, or a bare "—" SyncPill.
  it("case 6d: integrations resolved empty + syncStatus still PENDING never renders 'no flows synced yet'", () => {
    mocks.integrations.mockReturnValue(resolved([]));
    mocks.syncStatus.mockReturnValue(pending());
    const { container } = wrap(<CeligoIntegrationsPage />);
    expect(screen.queryByText(/no flows synced yet/i)).toBeNull();
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it("case 6e: integrations resolved with data + syncStatus still PENDING never renders tiles claiming 'on time' before stall data is known", () => {
    const maybeStalled = makeIntegration({
      flow_schedules: [flowSchedule({ last_executed_at: "2026-09-02T14:51:00.000Z" })],
    });
    mocks.integrations.mockReturnValue(resolved([maybeStalled]));
    mocks.syncStatus.mockReturnValue(pending());
    const { container } = wrap(<CeligoIntegrationsPage />);
    expect(screen.queryByText("Solidus + NetSuite")).toBeNull();
    expect(screen.queryByText("on time")).toBeNull();
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it("case 6f: SyncPill shows a loading indicator, not a bare '—', while syncStatus is pending", () => {
    mocks.integrations.mockReturnValue(resolved([makeIntegration()]));
    mocks.syncStatus.mockReturnValue(pending());
    wrap(<CeligoIntegrationsPage />);
    expect(screen.getByText(/checking sync status/i)).toBeInTheDocument();
  });

  it("case 6g: an errored syncStatus query shows a sync-status error, never the 'never synced' claim", () => {
    mocks.integrations.mockReturnValue(resolved([]));
    mocks.syncStatus.mockReturnValue(errored());
    wrap(<CeligoIntegrationsPage />);
    expect(screen.queryByText(/no flows synced yet/i)).toBeNull();
    expect(screen.getByText(/couldn.?t load sync status/i)).toBeInTheDocument();
  });

  it("case 6h: an errored syncStatus query's Retry calls its own refetch", () => {
    const refetch = vi.fn();
    mocks.integrations.mockReturnValue(resolved([makeIntegration()]));
    mocks.syncStatus.mockReturnValue(errored(refetch));
    wrap(<CeligoIntegrationsPage />);
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });

  // Fix round 1, finding 2: the list view's Last Run cell rendered only the
  // relative time, dropping the second (schedule/attention) pill the tile
  // view and the mockup's own list-view table both carry.
  it("finding 2: list view's Last Run cell carries the attention pill for every row", () => {
    routeMocks.view = "list";
    const stalled = makeIntegration({
      id: "int-stalled-list",
      name: "Stalled List Co",
      flow_schedules: [flowSchedule({ last_executed_at: "2026-09-02T14:51:00.000Z" })],
    });
    const onTime = makeIntegration({ id: "int-ontime-list", name: "On Time List Co", flow_schedules: [] });
    mocks.integrations.mockReturnValue(resolved([stalled, onTime]));
    wrap(<CeligoIntegrationsPage />);

    const table = screen.getByRole("table");
    const stalledRow = within(table).getByText("Stalled List Co").closest("tr") as HTMLElement;
    expect(within(stalledRow).getByText("stalled? 1 flow")).toBeInTheDocument();

    const onTimeRow = within(table).getByText("On Time List Co").closest("tr") as HTMLElement;
    expect(within(onTimeRow).getByText("on time")).toBeInTheDocument();
  });
});
