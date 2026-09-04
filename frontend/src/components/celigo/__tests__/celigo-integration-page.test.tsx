import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import type { CeligoIntegration, CeligoFlowSummary } from "@/hooks/use-celigo-flows";
import { resolved, pending, errored } from "./query-fixtures";

// Task 12 — the integration page (mockup screen 2): header, tabs, grouped
// flows table with the topology glyph and schedule/stall pills, and the
// per-step errors drawer. Mocks the hooks module (Task 10's established
// pattern) and the route module (Task 10's `routeMocks` pattern, one level
// up: the route hook itself is stubbed, not next/navigation).

const mocks = vi.hoisted(() => ({
  integrations: vi.fn(),
  flows: vi.fn(),
  syncStatus: vi.fn(),
  changes: vi.fn(),
  flowErrors: vi.fn(),
}));

vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoIntegrations: () => mocks.integrations(),
  useCeligoIntegrationFlows: () => mocks.flows(),
  useCeligoSyncStatus: () => mocks.syncStatus(),
  useCeligoIntegrationChanges: () => mocks.changes(),
  useCeligoFlowErrors: () => mocks.flowErrors(),
}));

const routeMocks = vi.hoisted(() => ({
  integrationId: "int-1" as string | null,
  tab: "flows" as "flows" | "scripts" | "errors" | "changes",
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
    view: "tiles" as const,
    integrationId: routeMocks.integrationId,
    tab: routeMocks.tab,
    flowId: null,
    stepId: null,
    scriptId: null,
    go: routeMocks.go,
  }),
}));

import { CeligoIntegrationPage, groupFlows, topologyGlyph } from "../celigo-integration-page";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// Real wall clock during the test — deliberately 2h after the sync so that
// any cell wrongly computed against Date.now() instead of the SYNC
// timestamp reads "2 h ago", not "21 min ago" (see `formatSchedule`'s and
// `stallState`'s own "never the wall clock" doctrine).
const NOW = new Date("2026-09-02T20:12:00.000Z");
// `last_synced_at` — the sync completed at 18:12 UTC.
const SYNCED_AT = "2026-09-02T18:12:00.000Z";
// A flow's own last run, 21 min before the sync — matches the mockup's own
// "New Sales Order to NetSuite - Multi-Subsidiary" numbers exactly.
const FLOW_LAST_RUN = "2026-09-02T17:51:00.000Z";
// A flow's own `errors_checked_at`, deliberately NOT the same moment as
// SYNCED_AT (2 h before NOW) — a flow's own error check can run at a
// different time than the enclosing sync's `last_synced_at`, and the Errors
// column's zero pill must read off THIS field, not the sync.
const FLOW_CHECKED_AT = "2026-09-02T19:51:00.000Z"; // 21 min before NOW

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
    // Same moment as SYNCED_AT: the ErrorsTab's quiet-errors sentence now
    // reads its "as of the last check" off THIS field (the integration
    // summary's own value), not `lastSyncedAt` -- see the honesty brief. A
    // test needing the "not checked yet" copy overrides it to `null`.
    errors_checked_at: SYNCED_AT,
    changes_last_24h: 0,
    last_run_at: "2026-09-02T18:06:00.000Z",
    writes: [
      { record_type: "salesorder", count: 19 },
      { record_type: "customer", count: 9 },
      { record_type: "itemfulfillment", count: 2 },
      { record_type: "customerdeposit", count: 2 },
      { record_type: "customrecord_a", count: 1 },
      { record_type: "customrecord_b", count: 1 },
    ],
    adaptor_families: ["HTTP", "NetSuite", "RDBMS"],
    flow_schedules: [],
    ...overrides,
  };
}

function makeFlow(overrides: Partial<CeligoFlowSummary> = {}): CeligoFlowSummary {
  return {
    id: "flow-1",
    celigo_id: "cel-flow-1",
    name: "Some Flow",
    disabled: false,
    schedule: "? 5,20,35,50 0-23 ? * *", // every 15 min, all hours
    timezone: "America/Los_Angeles",
    last_executed_at: FLOW_LAST_RUN,
    error_count: 0,
    signature_count: 0,
    errors_checked_at: null,
    step_count: 3,
    router_count: 0,
    branch_count: 0,
    lookup_count: 0,
    script_count: 0,
    diverged_family_count: 0,
    writes: [],
    celigo_last_modified: "2026-09-01T00:00:00.000Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  mocks.integrations.mockReset().mockReturnValue(resolved([makeIntegration()]));
  mocks.flows.mockReset().mockReturnValue(resolved([]));
  mocks.syncStatus.mockReset().mockReturnValue(resolved({ last_synced_at: SYNCED_AT }));
  mocks.changes.mockReset().mockReturnValue(resolved([]));
  mocks.flowErrors.mockReset().mockReturnValue(pending());
  routeMocks.integrationId = "int-1";
  routeMocks.tab = "flows";
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

function setup(flows: CeligoFlowSummary[] = []) {
  mocks.flows.mockReturnValue(resolved(flows));
  return wrap(<CeligoIntegrationPage />);
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe("topologyGlyph", () => {
  it("carries router_count/step_count straight off the flow summary", () => {
    expect(topologyGlyph({ router_count: 2, step_count: 10 })).toEqual({ routers: 2, steps: 10 });
    expect(topologyGlyph({ router_count: 0, step_count: 3 })).toEqual({ routers: 0, steps: 3 });
  });
});

describe("groupFlows", () => {
  it("groups scheduled, on-demand, then paused flows, in that order, skipping empty groups", () => {
    const scheduled = makeFlow({ id: "f-sched", disabled: false, schedule: "? 0,15,30,45 * ? * *" });
    const onDemand = makeFlow({ id: "f-ondemand", disabled: false, schedule: null });
    const paused = makeFlow({ id: "f-paused", disabled: true, schedule: "? 0 6 ? * *" });
    // Passed out of order — groupFlows imposes the fixed order, not input order.
    const groups = groupFlows([onDemand, paused, scheduled]);
    expect(groups.map((g) => g.key)).toEqual(["scheduled", "on_demand", "paused"]);
    expect(groups[0].label).toBe("On · scheduled · 1");
    expect(groups[0].flows).toEqual([scheduled]);
    expect(groups[1].label).toBe("On · on demand · 1");
    expect(groups[1].flows).toEqual([onDemand]);
    expect(groups[2].label).toBe("Paused in Celigo · 1");
    expect(groups[2].flows).toEqual([paused]);
  });

  it("omits a group entirely when it has no flows", () => {
    const scheduled = makeFlow({ id: "f-sched", disabled: false, schedule: "? 0,15,30,45 * ? * *" });
    expect(groupFlows([scheduled]).map((g) => g.key)).toEqual(["scheduled"]);
  });

  it("treats a disabled flow as paused regardless of its own schedule shape", () => {
    const paused = makeFlow({ disabled: true, schedule: null });
    expect(groupFlows([paused])[0].key).toBe("paused");
  });
});

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

describe("CeligoIntegrationPage — header", () => {
  it("shows the name, Production, medallions, the counts line, and the writes line", () => {
    setup([]);
    expect(screen.getByRole("heading", { name: "Solidus + NetSuite" })).toBeInTheDocument();
    expect(screen.getByText("Production")).toBeInTheDocument();
    expect(
      screen.getByText(
        "20 flows · 9 scheduled · 6 on demand · 5 paused · 94 steps · 11 routers · 24 lookups · 30 scripts",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "writes salesorder ×19 · customer ×9 · itemfulfillment ×2 · customerdeposit ×2 · +2 custom records",
      ),
    ).toBeInTheDocument();
  });

  it("shows Flows/Scripts/Errors/Changes tab counts", () => {
    mocks.changes.mockReturnValue(resolved([
      {
        id: "c1",
        object_kind: "flow",
        object_id: "flow-2",
        celigo_id: "cel-c1",
        field: "schedule",
        old_value: "? 0 6 ? * *",
        new_value: null,
        flow_id: "flow-2",
        created_at: SYNCED_AT,
      },
    ]));
    setup([
      makeFlow({ id: "flow-1", script_count: 2 }),
      makeFlow({ id: "flow-2", script_count: 3, error_count: 5, signature_count: 1 }),
    ]);
    expect(screen.getByRole("tab", { name: /Flows/ })).toHaveTextContent("Flows 2");
    expect(screen.getByRole("tab", { name: /Scripts/ })).toHaveTextContent("Scripts 5");
    expect(screen.getByRole("tab", { name: /Errors/ })).toHaveTextContent("Errors 1");
    expect(screen.getByRole("tab", { name: /Changes/ })).toHaveTextContent("Changes 1");
  });

  it("breadcrumb reads My integrations › Solidus + NetSuite, and clicking My integrations navigates there", () => {
    setup([]);
    expect(screen.getByRole("button", { name: "My integrations" })).toBeInTheDocument();
    expect(screen.getByText("Solidus + NetSuite", { selector: "b" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "My integrations" }));
    expect(routeMocks.go.integrations).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Flows table — grouping, schedule cells, paused rows
// ---------------------------------------------------------------------------

describe("flows table — grouping and schedule cells", () => {
  it("groups rows under On · scheduled, On · on demand, then Paused in Celigo", () => {
    setup([
      makeFlow({ id: "f-sched", name: "Scheduled Flow", disabled: false, schedule: "? 5,20,35,50 0-23 ? * *" }),
      makeFlow({ id: "f-ondemand", name: "On Demand Flow", disabled: false, schedule: null }),
      makeFlow({ id: "f-paused", name: "Paused Flow", disabled: true, schedule: "? 5 6 ? * *" }),
    ]);
    expect(screen.getByText("On · scheduled · 1")).toBeInTheDocument();
    expect(screen.getByText("On · on demand · 1")).toBeInTheDocument();
    expect(screen.getByText("Paused in Celigo · 1")).toBeInTheDocument();
  });

  it("marks a paused row with data-paused and a Paused state pill", () => {
    setup([makeFlow({ id: "f-paused", name: "Paused Flow", disabled: true, schedule: null })]);
    const row = screen.getByText("Paused Flow").closest("tr")!;
    expect(row.getAttribute("data-paused")).toBe("true");
    expect(within(row).getByText("Paused")).toBeInTheDocument();
  });

  it("shows a humanised cron label plus the verbatim elided display string", () => {
    setup([makeFlow({ id: "f1", name: "Cron Flow", disabled: false, schedule: "? 5,20,35,50 0-23 ? * *" })]);
    const row = screen.getByText("Cron Flow").closest("tr")!;
    expect(within(row).getByText("every 15 min")).toBeInTheDocument();
    expect(within(row).getByText("? 5,20,35,50 0…23 ? * *")).toBeInTheDocument();
  });

  it("shows the raw schedule string, and nothing else, for an unrecognised shape", () => {
    setup([makeFlow({ id: "f1", name: "Weird Flow", disabled: false, schedule: "0 0 1 1 *" })]);
    const row = screen.getByText("Weird Flow").closest("tr")!;
    expect(within(row).getByText("0 0 1 1 *")).toBeInTheDocument();
    expect(within(row).queryByText(/every|hourly|daily|×\/day/)).not.toBeInTheDocument();
  });

  it("shows 'on demand' for a null schedule", () => {
    setup([makeFlow({ id: "f1", name: "OnDemand Flow", disabled: false, schedule: null })]);
    const row = screen.getByText("OnDemand Flow").closest("tr")!;
    expect(within(row).getByText("on demand")).toBeInTheDocument();
  });

  // Codex fix wave, item 10. A zero is a claim, and on this surface every
  // claim carries the moment it was checked (`shared.tsx`'s ErrorPill says
  // "0 open errors · checked 4 min ago" for exactly this reason). The table's
  // own zero pill originally said a bare "0" titled off the overall sync
  // time — the exact honesty bug this pill now must not reproduce: a flow's
  // OWN `errors_checked_at` is the source, never `lastSyncedAt`.
  it("item 10: the Errors column's zero pill carries the flow's OWN errors_checked_at, not the sync time", () => {
    setup([
      makeFlow({
        id: "f1",
        name: "Clean Flow",
        disabled: false,
        error_count: 0,
        errors_checked_at: FLOW_CHECKED_AT,
      }),
    ]);

    expect(screen.getByRole("columnheader", { name: "Errors" })).toBeInTheDocument();
    const row = screen.getByText("Clean Flow").closest("tr")!;
    // FLOW_CHECKED_AT is 21 min before NOW; SYNCED_AT (the sync) is 2 h
    // before NOW. Asserting "21 min ago" (not "2 h ago") proves the source
    // really is the flow's own field, not lastSyncedAt.
    expect(within(row).getByText(/0 open errors/)).toBeInTheDocument();
    expect(within(row).getByText(/checked 21 min ago/)).toBeInTheDocument();
  });

  it("item 10: the zero pill still reads the flow's own errors_checked_at when the sync's own last_synced_at is null", () => {
    mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: null }));
    setup([
      makeFlow({
        id: "f1",
        name: "Clean Flow",
        disabled: false,
        error_count: 0,
        errors_checked_at: FLOW_CHECKED_AT,
      }),
    ]);

    const row = screen.getByText("Clean Flow").closest("tr")!;
    expect(within(row).getByText(/checked 21 min ago/)).toBeInTheDocument();
  });

  it("honesty: says 'errors not checked yet', never a green zero titled off the sync, when the flow's own errors_checked_at is null", () => {
    // The sync completed fine (last_synced_at is set, from the default mock)
    // -- only this flow's own error check never ran against the correct
    // endpoint. The old code read lastSyncedAt here and would have rendered
    // a false claim ("0 open errors as of the sync 2 h ago") even though
    // this flow itself was never checked.
    setup([
      makeFlow({ id: "f1", name: "Clean Flow", disabled: false, error_count: 0, errors_checked_at: null }),
    ]);

    const row = screen.getByText("Clean Flow").closest("tr")!;
    expect(within(row).getByText("errors not checked yet")).toBeInTheDocument();
    expect(within(row).queryByText(/0 open errors/)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Steps glyph
// ---------------------------------------------------------------------------

describe("flows table — steps glyph", () => {
  it("renders ◉→◇◇→10 for a flow with 2 routers and 10 steps", () => {
    setup([makeFlow({ id: "f1", name: "Multi-Router Flow", router_count: 2, step_count: 10 })]);
    const row = screen.getByText("Multi-Router Flow").closest("tr")!;
    expect(within(row).getByText("◉→◇◇→10")).toBeInTheDocument();
  });

  it("renders ◉→3 for a flow with no routers", () => {
    setup([makeFlow({ id: "f1", name: "Plain Flow", router_count: 0, step_count: 3 })]);
    const row = screen.getByText("Plain Flow").closest("tr")!;
    expect(within(row).getByText("◉→3")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Last run — relative to the SYNC timestamp, never the wall clock
// ---------------------------------------------------------------------------

describe("flows table — last run", () => {
  it("shows '21 min ago' and the on-time pill for a flow that ran on schedule", () => {
    setup([
      makeFlow({
        id: "f1",
        name: "OnTime Flow",
        schedule: "? 5,20,35,50 0-23 ? * *",
        last_executed_at: FLOW_LAST_RUN,
      }),
    ]);
    const row = screen.getByText("OnTime Flow").closest("tr")!;
    // Proves the sync timestamp is the reference clock, not Date.now() (real
    // now is 2h later — a wall-clock bug would read "2 h ago" here instead).
    expect(within(row).getByText("21 min ago")).toBeInTheDocument();
    expect(within(row).getByText("on time")).toBeInTheDocument();
  });

  it("shows a stalled pill with a missed-run count for a flow long overdue", () => {
    setup([
      makeFlow({
        id: "f1",
        name: "Stalled Flow",
        schedule: "? 0 6 ? * *", // daily 06:00
        last_executed_at: "2026-08-15T06:00:00.000Z",
      }),
    ]);
    const row = screen.getByText("Stalled Flow").closest("tr")!;
    expect(within(row).getByText(/stalled\? \d+ runs? missed/)).toBeInTheDocument();
  });

  it("shows 'no run recorded' when last_executed_at is null", () => {
    setup([
      makeFlow({
        id: "f1",
        name: "Never Ran Flow",
        schedule: "? 0 6 ? * *",
        last_executed_at: null,
      }),
    ]);
    const row = screen.getByText("Never Ran Flow").closest("tr")!;
    expect(within(row).getByText("no run recorded")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Last updated — a short absolute date (mockup: "26 Feb" / "2 Sep"), never
// a long relative string, and never computed off the wall clock (there is
// no "now" in an absolute date at all).
// ---------------------------------------------------------------------------

describe("flows table — last updated", () => {
  it("shows a short absolute date ('D MMM'), not a relative string", () => {
    setup([
      makeFlow({ id: "f1", name: "Modified Flow", celigo_last_modified: "2026-09-01T00:00:00.000Z" }),
    ]);
    const row = screen.getByText("Modified Flow").closest("tr")!;
    // Columns: Flow · Steps · Writes · Schedule · Last run · Last updated · Errors · Scripts · State
    const lastUpdatedCell = within(row).getAllByRole("cell")[5];
    // Real now is 2026-09-02T20:12Z and the sync completed 2026-09-02T18:12Z
    // — a wall-clock OR sync-clock relative bug would both render some
    // "N day(s) ago" string here; the fix renders an absolute date instead,
    // so neither clock can leak in.
    expect(lastUpdatedCell).toHaveTextContent("1 Sep");
    expect(lastUpdatedCell).not.toHaveTextContent(/ago/);
  });

  it("matches the mockup's own example exactly: a modification over a year old still reads '26 Feb', no year", () => {
    setup([
      makeFlow({ id: "f1", name: "Old Flow", celigo_last_modified: "2025-02-26T10:00:00.000Z" }),
    ]);
    const row = screen.getByText("Old Flow").closest("tr")!;
    expect(within(row).getByText("26 Feb")).toBeInTheDocument();
  });

  it("shows '—' when celigo_last_modified is null", () => {
    setup([makeFlow({ id: "f1", name: "Never Modified Flow", celigo_last_modified: null })]);
    const row = screen.getByText("Never Modified Flow").closest("tr")!;
    const cells = within(row).getAllByRole("cell");
    // Columns: Flow · Steps · Writes · Schedule · Last run · Last updated · Errors · Scripts · State
    expect(cells[5]).toHaveTextContent("—");
  });
});

// ---------------------------------------------------------------------------
// Scripts + Errors cells, and the per-step errors drawer
// ---------------------------------------------------------------------------

describe("flows table — scripts and errors cells", () => {
  it("shows the script count plus a diverged pill when any family diverged", () => {
    setup([makeFlow({ id: "f1", name: "Script Flow", script_count: 2, diverged_family_count: 1 })]);
    const row = screen.getByText("Script Flow").closest("tr")!;
    expect(within(row).getByText("2 · 1 diverged")).toBeInTheDocument();
  });

  it("shows a bare '0' for a flow with no scripts", () => {
    setup([makeFlow({ id: "f1", name: "No Script Flow", script_count: 0, diverged_family_count: 0 })]);
    const row = screen.getByText("No Script Flow").closest("tr")!;
    const cells = within(row).getAllByRole("cell");
    // Columns: Flow · Steps · Writes · Schedule · Last run · Last updated · Errors · Scripts · State
    expect(cells[7]).toHaveTextContent("0");
  });

  it("shows 'N open · M root cause(s)' for a flow with open errors, else shows '0 open errors' (checked) — error_count leads, matching the mockup and shared.tsx's ErrorPill convention", () => {
    setup([
      makeFlow({ id: "f1", name: "Errored Flow", error_count: 10, signature_count: 1 }),
      makeFlow({ id: "f2", name: "Clean Flow", error_count: 0, errors_checked_at: FLOW_CHECKED_AT }),
    ]);
    const erroredRow = screen.getByText("Errored Flow").closest("tr")!;
    const cleanRow = screen.getByText("Clean Flow").closest("tr")!;
    expect(within(erroredRow).getAllByRole("cell")[6]).toHaveTextContent("10 open · 1 root cause");
    expect(within(cleanRow).getAllByRole("cell")[6]).toHaveTextContent("0 open errors");
  });

  it("opens a 'Flow: name' drawer listing step_ids with counts when a non-zero errors count is clicked", () => {
    mocks.flowErrors.mockReturnValue(
      resolved({
        flow_id: "f1",
        status: "open" as const,
        total: 10,
        groups: [
          {
            signature: null,
            count: 10,
            step_ids: ["step-abc"],
            first_seen_at: null,
            last_seen_at: null,
            retriable: null,
            purge_at: null,
            trace_keys: [],
            errors: [],
          },
        ],
      }),
    );
    setup([makeFlow({ id: "f1", name: "Errored Flow", error_count: 10, signature_count: 1 })]);
    fireEvent.click(screen.getByText("10 open · 1 root cause"));
    const dialog = screen.getByRole("dialog", { name: "Flow: Errored Flow" });
    expect(within(dialog).getByText(/step-abc/)).toBeInTheDocument();
    expect(within(dialog).getByText(/10 errors/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Navigation — row click, tab switch
// ---------------------------------------------------------------------------

describe("navigation", () => {
  it("clicking a flow row opens the flow under THIS integration", () => {
    // Gate fix wave, item 5: every caller that knows which integration owns
    // the flow now says so, rather than letting `go.flow` guess from the URL.
    setup([makeFlow({ id: "f1", name: "Clickable Flow" })]);
    fireEvent.click(screen.getByText("Clickable Flow").closest("tr")!);
    expect(routeMocks.go.flow).toHaveBeenCalledWith("f1", "int-1");
  });

  it("a flow row is operable from the keyboard: focusable, announced as a link, Enter and Space navigate", () => {
    // Final-review finding I8. The row was a bare `<TableRow onClick>` -- no
    // role, no tab stop, no key handling -- so the whole flows table was
    // mouse-only. `role="link"` rather than "button" because activating it
    // navigates to the flow page; nothing is submitted or mutated.
    setup([makeFlow({ id: "f1", name: "Keyboard Flow" })]);
    const row = screen.getByText("Keyboard Flow").closest("tr")!;

    expect(row).toHaveAttribute("role", "link");
    expect(row).toHaveAttribute("tabindex", "0");
    expect(row.className).toMatch(/focus-visible:ring-2/);

    fireEvent.keyDown(row, { key: "Enter" });
    expect(routeMocks.go.flow).toHaveBeenCalledWith("f1", "int-1");

    routeMocks.go.flow.mockClear();
    fireEvent.keyDown(row, { key: " " });
    expect(routeMocks.go.flow).toHaveBeenCalledWith("f1", "int-1");
  });

  it("a key press on the errors button inside a row does not also navigate to the flow", () => {
    setup([makeFlow({ id: "f1", name: "Keyboard Flow", error_count: 3, signature_count: 1 })]);
    const row = screen.getByText("Keyboard Flow").closest("tr")!;
    fireEvent.keyDown(within(row).getByText(/3 open/), { key: "Enter" });
    expect(routeMocks.go.flow).not.toHaveBeenCalled();
  });

  it("switching tabs replaces the tab param instead of pushing a page", () => {
    // Gate fix wave, item 7: a tab is a selection inside the page already on
    // screen, so it must not cost a Back press each.
    setup([]);
    // Radix's Tabs.Trigger activates on `mousedown`, not `click` — see
    // @radix-ui/react-tabs's own TabsTrigger.
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Changes/ }));
    expect(routeMocks.go.tab).toHaveBeenCalledWith("changes");
    expect(routeMocks.go.integration).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Changes tab
// ---------------------------------------------------------------------------

describe("Changes tab", () => {
  beforeEach(() => {
    routeMocks.tab = "changes";
  });

  it("lists change rows as field · old → new · relative time", () => {
    mocks.changes.mockReturnValue(
      resolved([
        {
          id: "c1",
          object_kind: "flow",
          object_id: "f1",
          celigo_id: "cel-1",
          field: "schedule",
          old_value: "? 0 6 ? * *",
          new_value: null,
          flow_id: "f1",
          created_at: FLOW_LAST_RUN,
        },
      ]),
    );
    setup([]);
    // FLOW_LAST_RUN is 2h21m before NOW → "2 h ago".
    expect(screen.getByText("schedule · ? 0 6 ? * * → — · 2 h ago")).toBeInTheDocument();
  });

  it("shows the empty state when there are no changes", () => {
    mocks.changes.mockReturnValue(resolved([]));
    setup([]);
    expect(
      screen.getByText("No configuration changes recorded since syncing began."),
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Scripts tab
// ---------------------------------------------------------------------------

describe("Scripts tab", () => {
  beforeEach(() => {
    routeMocks.tab = "scripts";
  });

  it("renders the summary sentence, plus only the flows with script_count > 0", () => {
    setup([
      makeFlow({ id: "f1", name: "Has Scripts", script_count: 2 }),
      makeFlow({ id: "f2", name: "No Scripts", script_count: 0 }),
    ]);
    expect(
      screen.getByText("2 scripts across 2 flows · the Scripts view ships separately"),
    ).toBeInTheDocument();
    expect(screen.getByText("Has Scripts")).toBeInTheDocument();
    expect(screen.queryByText("No Scripts")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Errors tab
// ---------------------------------------------------------------------------

describe("Errors tab", () => {
  beforeEach(() => {
    routeMocks.tab = "errors";
  });

  it("lists only the flows with open errors, pill reading 'N open · M root cause(s)'", () => {
    setup([
      makeFlow({ id: "f1", name: "Errored Flow", error_count: 5, signature_count: 1 }),
      makeFlow({ id: "f2", name: "Clean Flow", error_count: 0 }),
    ]);
    expect(screen.getByText("Errored Flow")).toBeInTheDocument();
    expect(screen.getByText("5 open · 1 root cause")).toBeInTheDocument();
    expect(screen.queryByText("Clean Flow")).not.toBeInTheDocument();
  });

  it("shows the quiet-errors sentence with the check's relative time when nothing is open", () => {
    // Backend fix (this branch): the sentence names when the CORRECT
    // per-flow/per-resource error endpoint was last asked, off the
    // integration summary's own `errors_checked_at` -- Celigo itself never
    // "reported" a zero, so that framing is gone. Default makeIntegration()
    // carries `errors_checked_at: SYNCED_AT` (2h before NOW).
    setup([makeFlow({ id: "f1", name: "Clean Flow", error_count: 0 })]);
    expect(
      screen.getByText("No open errors as of the last check, 2 h ago."),
    ).toBeInTheDocument();
  });

  it("says errors haven't been checked yet, never a stale 'reported 0', when errors_checked_at is null", () => {
    mocks.integrations.mockReturnValue(resolved([makeIntegration({ errors_checked_at: null })]));
    setup([makeFlow({ id: "f1", name: "Clean Flow", error_count: 0 })]);
    expect(
      screen.getByText("Open errors haven't been checked yet for every flow here; the next sync checks them."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/reported 0/)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Query states
// ---------------------------------------------------------------------------

describe("query states", () => {
  it("shows a not-in-last-sync message with a link back for an unknown integration id", () => {
    routeMocks.integrationId = "int-ghost";
    setup([]);
    expect(screen.getByText("This integration is not in the last sync.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to My integrations" }));
    expect(routeMocks.go.integrations).toHaveBeenCalled();
  });

  it("shows an error notice, not a spinner, when the flows query fails", () => {
    mocks.flows.mockReturnValue(errored());
    wrap(<CeligoIntegrationPage />);
    expect(screen.getByText(/couldn.t load flows/i)).toBeInTheDocument();
  });

  it("shows an error notice on the Flows tab when sync status fails, never lastSyncedAt: null pills", () => {
    mocks.flows.mockReturnValue(resolved([makeFlow({ id: "f1", name: "Some Flow" })]));
    mocks.syncStatus.mockReturnValue(errored());
    wrap(<CeligoIntegrationPage />);
    expect(screen.getByText(/couldn.t load flows/i)).toBeInTheDocument();
    expect(screen.queryByText("Some Flow")).not.toBeInTheDocument();
  });

  it("shows skeleton rows while flows are pending, never an empty table", () => {
    mocks.flows.mockReturnValue(pending());
    wrap(<CeligoIntegrationPage />);
    expect(screen.getByText("Loading flows…")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText(/^On · scheduled/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Paused in Celigo/)).not.toBeInTheDocument();
  });
});
