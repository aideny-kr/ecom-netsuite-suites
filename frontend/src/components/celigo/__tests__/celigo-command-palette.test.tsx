import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, within, act } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { CeligoIntegration, CeligoFlowSchedule } from "@/hooks/use-celigo-flows";
import { resolved } from "./query-fixtures";

// Task 11 — ⌘K command palette over every integration and flow (mockup:
// "⌘K is a real palette over all 122 flows, each result carrying its health
// dot and integration"). Mocks the hooks module (Task 10's established
// pattern) and the route module (Task 9's own celigo-route.test.tsx nav
// mock: the route hook itself is stubbed, not next/navigation).

// cmdk scrolls the (re)selected item into view on every filter/selection
// change; jsdom has no `scrollIntoView` at all (not even a no-op), so cmdk's
// own internal effect throws without this stub. A test-environment concern,
// not a page concern, so it lives here rather than the shared
// vitest.setup.ts (which no other Celigo test needs).
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

const mocks = vi.hoisted(() => ({
  integrations: vi.fn(),
  syncStatus: vi.fn(),
}));

vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoIntegrations: () => mocks.integrations(),
  useCeligoSyncStatus: () => mocks.syncStatus(),
}));

const routeMocks = vi.hoisted(() => ({
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
    integrationId: null,
    tab: "flows" as const,
    flowId: null,
    stepId: null,
    scriptId: null,
    go: routeMocks.go,
  }),
}));

import { CeligoCommandPalette } from "../celigo-command-palette";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// Sync completed 17:51 UTC; a flow's own default fixture last ran one minute
// earlier -- on pace for its own "every 15 min" schedule (same numbers/shape
// as celigo-integrations-page.test.tsx's own flowSchedule default).
const SYNCED_AT = "2026-09-02T17:51:00.000Z";

function flowSchedule(overrides: Partial<CeligoFlowSchedule> = {}): CeligoFlowSchedule {
  return {
    id: "fs-1",
    name: "Some Flow",
    disabled: false,
    schedule: "? 0,15,30,45 * ? * *", // every 15 min, all hours
    last_executed_at: "2026-09-02T17:50:00.000Z", // 1 min before sync -> on_time
    ...overrides,
  };
}

function makeIntegration(overrides: Partial<CeligoIntegration> = {}): CeligoIntegration {
  return {
    id: "int-1",
    celigo_id: "c-int-1",
    name: "Solidus + NetSuite",
    sandbox: false,
    mode: "settings",
    description: null,
    celigo_last_modified: null,
    flow_count: 0,
    scheduled_count: 0,
    on_demand_count: 0,
    paused_count: 0,
    step_count: 0,
    router_count: 0,
    lookup_count: 0,
    script_count: 0,
    no_run_count: 0,
    error_count: 0,
    signature_count: 0,
    changes_last_24h: 0,
    last_run_at: null,
    writes: [],
    adaptor_families: [],
    flow_schedules: [],
    ...overrides,
  };
}

function openPalette() {
  // Task 9 dispatches this as a plain DOM CustomEvent (not through a React
  // synthetic handler), so the resulting setState needs an explicit `act`
  // to flush before the next assertion -- otherwise the render lands after
  // this function returns and every query below sees the pre-open DOM.
  act(() => {
    window.dispatchEvent(new CustomEvent("celigo:command-k"));
  });
}

beforeEach(() => {
  mocks.integrations.mockReset();
  mocks.syncStatus.mockReset();
  mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: SYNCED_AT }));
  routeMocks.go.files.mockReset();
  routeMocks.go.integrations.mockReset();
  routeMocks.go.integration.mockReset();
  routeMocks.go.flow.mockReset();
  routeMocks.go.step.mockReset();
  routeMocks.go.script.mockReset();
});

describe("CeligoCommandPalette", () => {
  it("case 1: the celigo:command-k window event opens the dialog with the search input", () => {
    mocks.integrations.mockReturnValue(resolved([makeIntegration()]));
    wrap(<CeligoCommandPalette />);

    expect(screen.queryByRole("dialog")).toBeNull();
    openPalette();

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Search integrations & flows")).toBeInTheDocument();
  });

  it("case 2: typing filters to a flow, showing its integration name and stall dot; Enter navigates to it", () => {
    const multi = flowSchedule({ id: "f1", name: "New Sales Order to NetSuite - Multi-Subsidiary" });
    const other = flowSchedule({ id: "f2", name: "Update Customer Balances" });
    const integration = makeIntegration({
      id: "int-1",
      name: "Solidus + NetSuite",
      flow_schedules: [multi, other],
    });
    mocks.integrations.mockReturnValue(resolved([integration]));
    wrap(<CeligoCommandPalette />);
    openPalette();

    fireEvent.change(screen.getByPlaceholderText("Search integrations & flows"), {
      target: { value: "Multi" },
    });

    expect(screen.queryByText("Update Customer Balances")).toBeNull();
    const row = screen
      .getByText("New Sales Order to NetSuite - Multi-Subsidiary")
      .closest('[cmdk-item]') as HTMLElement;
    expect(within(row).getByText("Solidus + NetSuite")).toBeInTheDocument();
    const dot = row.querySelector("[data-state]");
    expect(dot).toHaveAttribute("data-state", "on_time");

    fireEvent.keyDown(screen.getByPlaceholderText("Search integrations & flows"), { key: "Enter" });
    expect(routeMocks.go.flow).toHaveBeenCalledWith("f1");
  });

  it("case 3: an integration result calls go.integration(id)", () => {
    const integration = makeIntegration({
      id: "int-click-me",
      name: "Backfills Integration",
      flow_schedules: [],
    });
    mocks.integrations.mockReturnValue(resolved([integration]));
    wrap(<CeligoCommandPalette />);
    openPalette();

    fireEvent.click(screen.getByText("Backfills Integration"));
    expect(routeMocks.go.integration).toHaveBeenCalledWith("int-click-me");
  });

  it("case 4: Escape closes; renders names only -- no <pre>, and a decoy name renders as a name, nothing more", () => {
    // The decoy stands in for "a step's script name": it's fine that this
    // renders -- it IS the flow's own `name` field, which is exactly what
    // Names Only permits -- but it must never render through the script
    // viewer's own inert-code path (a <pre>), which would mean this surface
    // started treating a string as script CONTENT rather than a name.
    const decoyName = "preMap.js -- function body deliberately not shown here";
    const flow = flowSchedule({ id: "f-decoy", name: decoyName });
    const integration = makeIntegration({ flow_schedules: [flow] });
    mocks.integrations.mockReturnValue(resolved([integration]));
    const { container } = wrap(<CeligoCommandPalette />);
    openPalette();

    expect(screen.getByText(decoyName)).toBeInTheDocument();
    expect(container.querySelector("pre")).toBeNull();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("case 5: duplicate flow names across different integrations navigate to the arrowed-to row, not whichever duplicate is first in the DOM", () => {
    // Review finding: Command.Item's `value` must be unique per item -- cmdk
    // uses a single global `state.value` string, compared per-item via
    // strict equality, as its sole notion of "the selected row" (both for
    // aria-selected and for what Enter activates). Two flows sharing a name
    // across two different integrations (or a sandbox/production pair of
    // integrations sharing a name -- this schema models that via
    // `sandbox: boolean`) must not collide on that string.
    const dup1 = flowSchedule({ id: "dup-1", name: "Sync Orders" });
    const dup2 = flowSchedule({ id: "dup-2", name: "Sync Orders" });
    const intA = makeIntegration({ id: "int-a", name: "Warehouse A", flow_schedules: [dup1] });
    const intB = makeIntegration({ id: "int-b", name: "Warehouse B", flow_schedules: [dup2] });
    mocks.integrations.mockReturnValue(resolved([intA, intB]));
    wrap(<CeligoCommandPalette />);
    openPalette();

    const input = screen.getByPlaceholderText("Search integrations & flows");
    // Filter to just the two duplicate flow rows (the integration rows,
    // "Warehouse A"/"Warehouse B", don't fuzzy-match "Sync Orders").
    fireEvent.change(input, { target: { value: "Sync Orders" } });
    expect(screen.getAllByText("Sync Orders")).toHaveLength(2);

    // Default selection lands on the first duplicate (dup-1). Arrow down
    // once to move selection to the second duplicate (dup-2), then Enter.
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });

    // Navigation must follow the row the user actually arrowed to.
    expect(routeMocks.go.flow).toHaveBeenCalledWith("dup-2");
    expect(routeMocks.go.flow).not.toHaveBeenCalledWith("dup-1");
  });
});
