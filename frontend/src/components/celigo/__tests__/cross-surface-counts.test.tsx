import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import type {
  CeligoIntegration,
  CeligoFlowSummary,
  CeligoFlowDetail,
  CeligoFlowStep,
  CeligoFlowErrors,
} from "@/hooks/use-celigo-flows";
import { resolved, pending } from "./query-fixtures";

// Task 18 — cross-surface error-count consistency. ONE mocked "world" (an
// integration, its one erroring flow, that flow's detail, and the grouped
// errors payload) rendered through all three top-level pages —
// `CeligoIntegrationsPage`, `CeligoIntegrationPage`, `CeligoFlowPage` — so a
// single source of truth (10 open errors, 1 distinct root cause, all on the
// lookup step) either reads the SAME everywhere, or this test catches the
// surface that disagrees before a customer does.
//
// Mocks the hooks module (every hook every one of the three pages calls —
// each page's own test file mocks a subset; this file needs the union) and
// the route module (Task 10's `routeMocks` pattern), exactly like the three
// pages' own test suites already do.

const mocks = vi.hoisted(() => ({
  integrations: vi.fn(),
  integrationFlows: vi.fn(),
  flowDetail: vi.fn(),
  syncStatus: vi.fn(),
  integrationChanges: vi.fn(),
  flowErrors: vi.fn(),
  script: vi.fn(),
}));

vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoIntegrations: () => mocks.integrations(),
  useCeligoIntegrationFlows: () => mocks.integrationFlows(),
  useCeligoFlowDetail: () => mocks.flowDetail(),
  useCeligoSyncStatus: () => mocks.syncStatus(),
  useCeligoIntegrationChanges: () => mocks.integrationChanges(),
  useCeligoFlowErrors: () => mocks.flowErrors(),
  useCeligoScript: (scriptId: string | undefined) => mocks.script(scriptId),
}));

const routeMocks = vi.hoisted(() => ({
  view: "tiles" as "tiles" | "list",
  integrationId: null as string | null,
  tab: "flows" as "flows" | "scripts" | "errors" | "changes",
  flowId: null as string | null,
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
    view: routeMocks.view,
    integrationId: routeMocks.integrationId,
    tab: routeMocks.tab,
    flowId: routeMocks.flowId,
    stepId: routeMocks.stepId,
    scriptId: routeMocks.scriptId,
    go: routeMocks.go,
  }),
}));

import { CeligoIntegrationsPage } from "../celigo-integrations-page";
import { CeligoIntegrationPage } from "../celigo-integration-page";
import { CeligoFlowPage } from "../celigo-flow-page";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const NOW = new Date("2026-09-02T18:33:00.000Z");
const SYNCED_AT = "2026-09-02T18:12:00.000Z";
// The flow's own last run, 1 min before the sync, on the standard 15-min
// cron below — genuinely "on time" by schedule ALONE, so the navigator
// dot's assertion below only passes if the error-based override actually
// fires, never by coincidence.
const FLOW_LAST_RUN = "2026-09-02T18:11:00.000Z";
const SCHEDULE = "? 5,20,35,50 0-23 ? * *"; // every 15 min, all hours

// The one root cause every surface must agree on: 10 open errors, 1 distinct
// signature, all attributed to the lookup step ("lookup customer · search
// 5090" — Global Constraints' own NetSuite-lookup fallback title).
const LOOKUP_STEP_ID = "step-lookup";
const FLOW_ID = "flow-1";
const INTEGRATION_ID = "int-1";

const LOOKUP_STEP: CeligoFlowStep = {
  id: LOOKUP_STEP_ID,
  celigo_id: "cel-step-lookup",
  role: "processor",
  router_id: null,
  branch_id: null,
  branch_key: "",
  sequence: 0,
  adaptor_type: "NetSuiteDistributedExport",
  connection_celigo_id: null,
  reference_name: null,
  filter_json: null,
  mapping_json: null,
  proceed_on_failure: null,
  skip_retries: null,
  kind: "lookup",
  record_type: "customer",
  operation: null,
  search_id: "5090",
  attachments: [],
  error_count: 10,
};

const INTEGRATION: CeligoIntegration = {
  id: INTEGRATION_ID,
  celigo_id: "c-int-1",
  name: "Solidus + NetSuite",
  sandbox: false,
  mode: "settings",
  description: null,
  celigo_last_modified: null,
  flow_count: 1,
  scheduled_count: 1,
  on_demand_count: 0,
  paused_count: 0,
  step_count: 1,
  router_count: 0,
  lookup_count: 1,
  script_count: 0,
  no_run_count: 0,
  error_count: 10,
  signature_count: 1,
  errors_checked_at: SYNCED_AT,
  changes_last_24h: 0,
  last_run_at: FLOW_LAST_RUN,
  writes: [],
  adaptor_families: ["NetSuite"],
  flow_schedules: [
    { id: FLOW_ID, name: "Erroring Flow", disabled: false, schedule: SCHEDULE, last_executed_at: FLOW_LAST_RUN },
  ],
};

const FLOW_SUMMARY: CeligoFlowSummary = {
  id: FLOW_ID,
  celigo_id: "cel-flow-1",
  name: "Erroring Flow",
  disabled: false,
  schedule: SCHEDULE,
  timezone: "America/Los_Angeles",
  last_executed_at: FLOW_LAST_RUN,
  error_count: 10,
  signature_count: 1,
  errors_checked_at: SYNCED_AT,
  step_count: 1,
  router_count: 0,
  branch_count: 0,
  lookup_count: 1,
  script_count: 0,
  diverged_family_count: 0,
  writes: [],
  celigo_last_modified: null,
};

const FLOW_DETAIL: CeligoFlowDetail = {
  id: FLOW_ID,
  integration_id: INTEGRATION_ID,
  celigo_id: "cel-flow-1",
  name: "Erroring Flow",
  disabled: false,
  schedule: SCHEDULE,
  timezone: "America/Los_Angeles",
  last_executed_at: FLOW_LAST_RUN,
  source_id: null,
  ai_description_summary: null,
  ai_description_detailed: null,
  celigo_last_modified: null,
  steps: [LOOKUP_STEP],
  unassigned_attachments: [],
  routers: [],
  celigo_open_error_count: 10,
  last_error_at: FLOW_LAST_RUN,
  error_count: 10,
  signature_count: 1,
  errors_checked_at: SYNCED_AT,
};

const FLOW_ERRORS: CeligoFlowErrors = {
  flow_id: FLOW_ID,
  status: "open",
  total: 10,
  groups: [
    {
      signature: {
        id: "sig-1",
        fingerprint: "fp-1",
        source: "import",
        code: "ERR001",
        sample_message: "Customer not found",
        occurrence_count: 10,
        first_seen: FLOW_LAST_RUN,
        last_seen: FLOW_LAST_RUN,
      },
      count: 10,
      step_ids: [LOOKUP_STEP_ID],
      first_seen_at: FLOW_LAST_RUN,
      last_seen_at: FLOW_LAST_RUN,
      retriable: true,
      purge_at: null,
      trace_keys: ["trace-1"],
      errors: [],
    },
  ],
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);

  mocks.integrations.mockReset().mockReturnValue(resolved([INTEGRATION]));
  mocks.integrationFlows.mockReset().mockReturnValue(resolved([FLOW_SUMMARY]));
  mocks.flowDetail.mockReset().mockReturnValue(resolved(FLOW_DETAIL));
  mocks.syncStatus.mockReset().mockReturnValue(resolved({ last_synced_at: SYNCED_AT }));
  mocks.integrationChanges.mockReset().mockReturnValue(resolved([]));
  mocks.flowErrors.mockReset().mockReturnValue(resolved(FLOW_ERRORS));
  mocks.script.mockReset().mockReturnValue(pending());

  routeMocks.view = "tiles";
  routeMocks.integrationId = INTEGRATION_ID;
  routeMocks.tab = "flows";
  routeMocks.flowId = FLOW_ID;
  routeMocks.stepId = LOOKUP_STEP_ID;
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

describe("cross-surface error-count consistency — one world, three pages", () => {
  it("the integrations-page tile pill reads '10 open · 1 root cause'", () => {
    wrap(<CeligoIntegrationsPage />);
    const tile = screen.getByText("Solidus + NetSuite").closest("button") as HTMLElement;
    expect(within(tile).getByText("10 open · 1 root cause")).toBeInTheDocument();
  });

  it("the integration page's flows-table Errors cell shows the same 10 open · 1 root cause", () => {
    wrap(<CeligoIntegrationPage />);
    const table = screen.getByRole("table");
    const row = within(table).getByText("Erroring Flow").closest("tr") as HTMLElement;
    expect(within(row).getByText("10 open · 1 root cause")).toBeInTheDocument();
  });

  it("the flow page's header pill shows the same 10 open · 1 root cause", () => {
    wrap(<CeligoFlowPage />);
    expect(screen.getByText("10 open · 1 root cause")).toBeInTheDocument();
  });

  it("the lookup step's bubble badge shows the same 10 open", () => {
    wrap(<CeligoFlowPage />);
    const bubble = screen.getByTestId(`step-bubble-${LOOKUP_STEP_ID}`);
    expect(within(bubble).getByText("10 open")).toBeInTheDocument();
  });

  it("the navigator dot for the erroring flow reads data-state=\"crit\" — not merely its schedule tone", () => {
    wrap(<CeligoFlowPage />);
    const rail = screen.getByTestId("celigo-nav-rail");
    const dot = rail.querySelector('span[data-state]') as HTMLElement;
    expect(dot).toHaveAttribute("data-state", "crit");
  });

  it("the step inspector's Errors tab shows the same 10, for the same step", () => {
    wrap(<CeligoFlowPage />);
    expect(screen.getByRole("tab", { name: "Errors 10" })).toBeInTheDocument();
  });
});
