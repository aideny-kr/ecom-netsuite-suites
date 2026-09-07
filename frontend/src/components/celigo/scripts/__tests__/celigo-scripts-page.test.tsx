import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { resolved, pending, errored } from "../../__tests__/query-fixtures";
import type { CeligoScriptFamiliesList, CeligoScriptFamilySummary } from "@/hooks/use-celigo-flows";

// Task 4 — the full page: the crumb + heading (Task 3's minimal shell,
// still covered below), five stat tiles wired to `filter=`, the
// list|detail split (list pane = `celigo-scripts-list.tsx`, mocked here so
// this file owns only the PAGE's own behaviour — the list's own grouping/
// search/keyboard rules are `celigo-scripts-list.test.tsx`'s job), and the
// three query/empty states (`queryState()` pending/error, "not synced yet").

const mocks = vi.hoisted(() => ({ families: vi.fn(), syncStatus: vi.fn() }));
vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoScriptFamilies: () => mocks.families(),
  useCeligoSyncStatus: () => mocks.syncStatus(),
}));

const routeMocks = vi.hoisted(() => ({
  go: { integrations: vi.fn(), scripts: vi.fn() },
  familyKey: null as string | null,
  scriptsFilter: "all" as string,
  scriptsKind: null as string | null,
  q: "" as string,
  scriptsIntegrationId: null as string | null,
  copyId: null as string | null,
}));
vi.mock("../../celigo-route", () => ({
  useCeligoRoute: () => ({
    familyKey: routeMocks.familyKey ?? null,
    scriptsFilter: routeMocks.scriptsFilter ?? "all",
    scriptsKind: routeMocks.scriptsKind ?? null,
    q: routeMocks.q ?? "",
    scriptsIntegrationId: routeMocks.scriptsIntegrationId ?? null,
    copyId: routeMocks.copyId ?? null,
    compare: null,
    go: routeMocks.go,
  }),
}));

// The list pane is a separate, separately-tested component — stubbed here
// with a `data-testid` that exposes exactly what this file needs to assert
// (which props it was given), so a tiles/empty-state test never breaks
// because of an unrelated change inside `celigo-scripts-list.tsx`.
const listMocks = vi.hoisted(() => ({ render: vi.fn() }));
vi.mock("../celigo-scripts-list", () => ({
  CeligoScriptsList: (props: Record<string, unknown>) => {
    listMocks.render(props);
    return <div data-testid="stub-scripts-list" />;
  },
}));

import { CeligoScriptsPage } from "../celigo-scripts-page";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const EMPTY_LIST: CeligoScriptFamiliesList = {
  totals: {
    scripts: 0,
    families: 0,
    attached_families: 0,
    unattached_families: 0,
    diverged_families: 0,
    sites: 0,
    flows_with_sites: 0,
    flows_total: 0,
    integrations_with_sites: 0,
    sites_with_open_errors: 0,
  },
  families: [],
  synced_at: null,
};

function makeFamily(overrides: Partial<CeligoScriptFamilySummary> = {}): CeligoScriptFamilySummary {
  return {
    dedup_key: "fam-1",
    name: "ns_sales_order_premap",
    kind: "hook",
    function_name: "preMap",
    copies_count: 7,
    versions_count: 3,
    content_diverged: true,
    original_present: true,
    sites_count: 16,
    flows_count: 8,
    integrations_count: 2,
    integration_ids: ["int-1", "int-2"],
    flow_names: ["Flow A"],
    sites_with_open_errors: 1,
    sites_unchecked: 0,
    first_modified: "2025-09-03T00:00:00Z",
    last_modified: "2026-06-30T00:00:00Z",
    max_size_bytes: 2400,
    other_families_with_name: 0,
    ...overrides,
  };
}

const SYNCED_LIST: CeligoScriptFamiliesList = {
  totals: {
    scripts: 129,
    families: 98,
    attached_families: 67,
    unattached_families: 31,
    diverged_families: 14,
    sites: 118,
    flows_with_sites: 56,
    flows_total: 122,
    integrations_with_sites: 12,
    sites_with_open_errors: 3,
  },
  families: [makeFamily()],
  synced_at: "2026-09-06T12:00:00Z",
};

beforeEach(() => {
  routeMocks.go.integrations.mockReset();
  routeMocks.go.scripts.mockReset();
  listMocks.render.mockReset();
  Object.assign(routeMocks, {
    familyKey: null,
    scriptsFilter: "all",
    scriptsKind: null,
    q: "",
    scriptsIntegrationId: null,
    copyId: null,
  });
  // Fix round 1, finding 1: every test above set only `mocks.families` and
  // left `useCeligoSyncStatus()` unmocked, which is why the crumb's own
  // "synced N ago" fact (spec §3.3) was never asserted anywhere in this
  // file. Defaulted to a resolved, recent sync so tests that don't care
  // about sync-status states aren't forced to stub it themselves.
  mocks.syncStatus.mockReset();
  mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: "2026-09-06T11:30:00Z" }));
});

describe("CeligoScriptsPage — shell (query states, crumb)", () => {
  it("always renders the Celigo › Scripts crumb and the Scripts heading", () => {
    mocks.families.mockReturnValue(pending());
    wrap(<CeligoScriptsPage />);
    expect(screen.getByText("Celigo")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Scripts" })).toBeInTheDocument();
  });

  // Fix round 1, finding 1: spec §3.3 requires the crumb row to carry
  // "synced-ago from sync status" — every sibling Celigo page renders this
  // off its OWN `useCeligoSyncStatus()` call, gated through `queryState()`
  // (never inferred from `lastSyncedAt` alone, same discipline as
  // `celigo-integrations-page.tsx`'s `SyncPill`), so it must show up here
  // too, independent of the families query's own `synced_at` (which only
  // gates the "not synced yet" empty state below).
  it("renders a synced-ago indicator sourced from the sync-status query", () => {
    mocks.families.mockReturnValue(resolved(SYNCED_LIST));
    mocks.syncStatus.mockReturnValue(resolved({ last_synced_at: "2026-09-06T11:30:00Z" }));
    wrap(<CeligoScriptsPage />);
    expect(screen.getByText(/synced.*ago/i)).toBeInTheDocument();
  });

  it("shows a checking-status indicator while the sync-status query is pending, never a bare dash", () => {
    mocks.families.mockReturnValue(resolved(SYNCED_LIST));
    mocks.syncStatus.mockReturnValue(pending());
    wrap(<CeligoScriptsPage />);
    expect(screen.getByText(/checking sync status/i)).toBeInTheDocument();
  });

  it("shows a sync-status-unavailable indicator when the sync-status query errors", () => {
    mocks.families.mockReturnValue(resolved(SYNCED_LIST));
    mocks.syncStatus.mockReturnValue(errored());
    wrap(<CeligoScriptsPage />);
    expect(screen.getByText(/sync status unavailable/i)).toBeInTheDocument();
  });

  it("pending renders a loading skeleton, never an empty one or a confident zero", () => {
    mocks.families.mockReturnValue(pending());
    wrap(<CeligoScriptsPage />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
  });

  it("error renders a retry notice, never loading or a confident zero", () => {
    const refetch = vi.fn();
    mocks.families.mockReturnValue(errored(refetch));
    wrap(<CeligoScriptsPage />);
    expect(screen.getByText(/couldn.?t load/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });

  it("clicking the Celigo crumb goes back to the integrations list", () => {
    mocks.families.mockReturnValue(resolved(SYNCED_LIST));
    wrap(<CeligoScriptsPage />);
    fireEvent.click(screen.getByText("Celigo"));
    expect(routeMocks.go.integrations).toHaveBeenCalled();
  });

  it("not-synced (synced_at === null) renders the exact spec §3.3 copy", () => {
    mocks.families.mockReturnValue(resolved(EMPTY_LIST));
    wrap(<CeligoScriptsPage />);
    expect(
      screen.getByText("Scripts have not been synced yet. Run the Celigo sync from Settings, then come back."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("stub-scripts-list")).not.toBeInTheDocument();
  });
});

describe("CeligoScriptsPage — stat tiles", () => {
  it("shows the five totals and clicking a tile sets that filter on the URL", () => {
    mocks.families.mockReturnValue(resolved(SYNCED_LIST));
    wrap(<CeligoScriptsPage />);
    expect(screen.getByText("129")).toBeInTheDocument(); // Scripts
    expect(screen.getByText("67")).toBeInTheDocument(); // Attached
    expect(screen.getByText("31")).toBeInTheDocument(); // Unattached
    expect(screen.getByText("14")).toBeInTheDocument(); // Diverged
    expect(screen.getByText("3")).toBeInTheDocument(); // Sites with open errors

    fireEvent.click(screen.getByRole("button", { name: /diverged/i }));
    expect(routeMocks.go.scripts).toHaveBeenCalledWith(expect.objectContaining({ filter: "diverged" }));
  });

  it("marks the tile matching the current URL filter as pressed", () => {
    mocks.families.mockReturnValue(resolved(SYNCED_LIST));
    Object.assign(routeMocks, { scriptsFilter: "errors" });
    wrap(<CeligoScriptsPage />);
    expect(screen.getByRole("button", { name: /sites with open errors/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: /^scripts/i })).toHaveAttribute("aria-pressed", "false");
  });
});

describe("CeligoScriptsPage — list pane wiring", () => {
  it("passes the families, totals, and every current filter through to the list pane", () => {
    mocks.families.mockReturnValue(resolved(SYNCED_LIST));
    Object.assign(routeMocks, { scriptsFilter: "diverged", q: "sales", familyKey: "fam-1" });
    wrap(<CeligoScriptsPage />);
    expect(listMocks.render).toHaveBeenCalledWith(
      expect.objectContaining({
        families: SYNCED_LIST.families,
        totals: SYNCED_LIST.totals,
        selectedKey: "fam-1",
        filter: "diverged",
        q: "sales",
      }),
    );
  });

  it("selecting a family calls go.scripts with that family and clears any stale copy", () => {
    mocks.families.mockReturnValue(resolved(SYNCED_LIST));
    Object.assign(routeMocks, { copyId: "some-old-copy" });
    wrap(<CeligoScriptsPage />);
    const onSelect = listMocks.render.mock.calls[0][0].onSelect as (key: string) => void;
    onSelect("fam-1");
    expect(routeMocks.go.scripts).toHaveBeenCalledWith(expect.objectContaining({ family: "fam-1", copy: null }));
  });
});

describe("CeligoScriptsPage — write-surface guard", () => {
  it("contains no button labelled deploy/push/save/edit/run, and no <form>", () => {
    mocks.families.mockReturnValue(resolved(SYNCED_LIST));
    const { container } = wrap(<CeligoScriptsPage />);
    expect(container.querySelector("form")).toBeNull();
    const labels = screen.getAllByRole("button").map((b) => b.textContent?.toLowerCase() ?? "");
    for (const label of labels) {
      expect(label).not.toMatch(/deploy|push|save|edit|\brun\b/);
    }
  });
});
