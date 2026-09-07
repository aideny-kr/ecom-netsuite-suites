import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { CeligoScriptFamilySummary, CeligoScriptFamilyTotals } from "@/hooks/use-celigo-flows";

// `CeligoScriptsList` calls `useCeligoIntegrations()` itself (to resolve the
// `integration_ids` on a family into names for the "Integration: any ▾"
// select — the list-level summary carries only ids, see
// `use-celigo-flows.ts`'s `CeligoScriptFamilySummary` docstring). Mocked so
// this file never needs a `QueryClientProvider`.
const mocks = vi.hoisted(() => ({ integrations: vi.fn() }));
vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoIntegrations: () => mocks.integrations(),
}));

import { CeligoScriptsList, filterFamilies, groupFamiliesByKind } from "../celigo-scripts-list";

function family(overrides: Partial<CeligoScriptFamilySummary>): CeligoScriptFamilySummary {
  return {
    dedup_key: "fam-default",
    name: "default_script",
    kind: "hook",
    function_name: null,
    copies_count: 1,
    versions_count: 1,
    content_diverged: false,
    original_present: true,
    sites_count: 0,
    flows_count: 0,
    integrations_count: 0,
    integration_ids: [],
    flow_names: [],
    sites_with_open_errors: 0,
    sites_unchecked: 0,
    first_modified: null,
    last_modified: null,
    max_size_bytes: null,
    other_families_with_name: 0,
    ...overrides,
  };
}

const TOTALS: CeligoScriptFamilyTotals = {
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
};

const HOOK_A = family({
  dedup_key: "fam-hook-a",
  name: "ns_sales_order_premap",
  kind: "hook",
  function_name: "preMap",
  copies_count: 7,
  versions_count: 3,
  content_diverged: true,
  sites_count: 16,
  flows_count: 8,
  integration_ids: ["int-1", "int-2"],
  flow_names: ["New Sales Order to NetSuite", "Backfill Sales Order"],
});
const HOOK_B = family({
  dedup_key: "fam-hook-b",
  name: "sales_order_script_v2",
  kind: "hook",
  function_name: "preSavePage",
  copies_count: 1,
  versions_count: 1,
  sites_count: 4,
  flows_count: 4,
  integration_ids: ["int-1"],
  flow_names: ["Update Pre-Orders"],
});
const TRANSFORM_A = family({
  dedup_key: "fam-transform-a",
  name: "service_stock_sync_transform",
  kind: "transform",
  sites_count: 4,
  flows_count: 2,
  integration_ids: ["int-2"],
  flow_names: ["Stock Sync"],
});
const UNATTACHED_A = family({
  dedup_key: "fam-unattached-a",
  name: "Framework 945 v2",
  kind: "unattached",
  function_name: "postResponseMap",
  copies_count: 2,
  versions_count: 2,
  content_diverged: true,
  other_families_with_name: 2,
});

const ALL_FAMILIES = [HOOK_A, HOOK_B, TRANSFORM_A, UNATTACHED_A];

function noop() {
  /* not asserted in most tests */
}

function baseProps(overrides: Partial<React.ComponentProps<typeof CeligoScriptsList>> = {}) {
  return {
    families: ALL_FAMILIES,
    totals: { ...TOTALS, families: ALL_FAMILIES.length },
    selectedKey: null,
    onSelect: vi.fn(),
    filter: "all" as const,
    kind: null,
    q: "",
    integrationId: null,
    onFilterChange: vi.fn(),
    onKindChange: vi.fn(),
    onQueryChange: vi.fn(),
    onIntegrationChange: vi.fn(),
    ...overrides,
  };
}

beforeEach(() => {
  mocks.integrations.mockReturnValue({ data: [{ id: "int-1", name: "Solidus + NetSuite" }, { id: "int-2", name: "Backfills" }] });
});

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe("filterFamilies", () => {
  it("filter=attached keeps only families with at least one site", () => {
    const out = filterFamilies(ALL_FAMILIES, { filter: "attached", kind: null, q: "", integrationId: null });
    expect(out.map((f) => f.dedup_key)).toEqual(["fam-hook-a", "fam-hook-b", "fam-transform-a"]);
  });

  it("filter=unattached keeps only families with zero sites", () => {
    const out = filterFamilies(ALL_FAMILIES, { filter: "unattached", kind: null, q: "", integrationId: null });
    expect(out.map((f) => f.dedup_key)).toEqual(["fam-unattached-a"]);
  });

  it("filter=diverged keeps only content_diverged families", () => {
    const out = filterFamilies(ALL_FAMILIES, { filter: "diverged", kind: null, q: "", integrationId: null });
    expect(out.map((f) => f.dedup_key)).toEqual(["fam-hook-a", "fam-unattached-a"]);
  });

  it("filter=errors keeps only families with sites_with_open_errors > 0", () => {
    const withError = family({ dedup_key: "fam-err", sites_count: 1, sites_with_open_errors: 1 });
    const out = filterFamilies([...ALL_FAMILIES, withError], {
      filter: "errors",
      kind: null,
      q: "",
      integrationId: null,
    });
    expect(out.map((f) => f.dedup_key)).toEqual(["fam-err"]);
  });

  it("kind filters exactly on the family's kind", () => {
    const out = filterFamilies(ALL_FAMILIES, { filter: "all", kind: "transform", q: "", integrationId: null });
    expect(out.map((f) => f.dedup_key)).toEqual(["fam-transform-a"]);
  });

  it("integrationId keeps only families whose integration_ids includes it", () => {
    const out = filterFamilies(ALL_FAMILIES, { filter: "all", kind: null, q: "", integrationId: "int-2" });
    expect(out.map((f) => f.dedup_key)).toEqual(["fam-hook-a", "fam-transform-a"]);
  });

  it("q matches name, function_name, and flow_names, case-insensitively", () => {
    expect(
      filterFamilies(ALL_FAMILIES, { filter: "all", kind: null, q: "SALES_ORDER", integrationId: null }).map(
        (f) => f.dedup_key,
      ),
    ).toEqual(["fam-hook-a", "fam-hook-b"]);
    expect(
      filterFamilies(ALL_FAMILIES, { filter: "all", kind: null, q: "presavepage", integrationId: null }).map(
        (f) => f.dedup_key,
      ),
    ).toEqual(["fam-hook-b"]);
    expect(
      filterFamilies(ALL_FAMILIES, { filter: "all", kind: null, q: "stock sync", integrationId: null }).map(
        (f) => f.dedup_key,
      ),
    ).toEqual(["fam-transform-a"]);
  });
});

describe("groupFamiliesByKind", () => {
  it("orders groups Hooks, Transforms, Filters, Routers, Mixed, Unattached, omitting empty groups", () => {
    const groups = groupFamiliesByKind(ALL_FAMILIES);
    expect(groups.map((g) => g.key)).toEqual(["hook", "transform", "unattached"]);
  });

  it("each group header states its family count and, for attached kinds, its site count", () => {
    const groups = groupFamiliesByKind(ALL_FAMILIES);
    const hooks = groups.find((g) => g.key === "hook")!;
    expect(hooks.label).toMatch(/hooks/i);
    expect(hooks.label).toContain("20 sites"); // 16 + 4
    expect(hooks.label).toContain("2 families");
  });

  it("the unattached group counts scripts (copies), not sites", () => {
    const groups = groupFamiliesByKind(ALL_FAMILIES);
    const unattached = groups.find((g) => g.key === "unattached")!;
    expect(unattached.label).toContain("2 scripts");
    expect(unattached.label).toContain("1 family");
  });

  it("keeps each group's own family order", () => {
    const groups = groupFamiliesByKind(ALL_FAMILIES);
    const hooks = groups.find((g) => g.key === "hook")!;
    expect(hooks.families.map((f) => f.dedup_key)).toEqual(["fam-hook-a", "fam-hook-b"]);
  });
});

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

describe("CeligoScriptsList", () => {
  it("renders groups in the fixed kind order with header counts, each family in its group", () => {
    render(<CeligoScriptsList {...baseProps()} />);
    const headers = screen.getAllByTestId("family-group-header").map((el) => el.textContent);
    expect(headers).toEqual([
      "Hooks · 20 sites · 2 families",
      "Transforms · 4 sites · 1 family",
      "Unattached · 2 scripts · 1 family",
    ]);
    expect(screen.getByText("ns_sales_order_premap")).toBeInTheDocument();
    expect(screen.getByText("Framework 945 v2")).toBeInTheDocument();
  });

  it("shows the copies pill amber (×N · V versions) only when the family diverged", () => {
    render(<CeligoScriptsList {...baseProps()} />);
    expect(screen.getByText("×7 · 3 versions")).toBeInTheDocument();
    expect(screen.getAllByText("×1")).toHaveLength(2); // HOOK_B and TRANSFORM_A both have 1 undiverged copy
    expect(screen.getByText("×2 · 2 versions")).toBeInTheDocument();
  });

  it('shows "N families with this name" only when other_families_with_name > 0', () => {
    render(<CeligoScriptsList {...baseProps()} />);
    expect(screen.getByText(/2 families with this name/i)).toBeInTheDocument();
    // The other three families all have other_families_with_name === 0.
    expect(screen.queryByText(/0 families with this name/i)).not.toBeInTheDocument();
  });

  it("typing in the search box calls onQueryChange with the typed text", () => {
    const onQueryChange = vi.fn();
    render(<CeligoScriptsList {...baseProps({ onQueryChange })} />);
    fireEvent.change(screen.getByPlaceholderText("Search scripts, functions, flows"), {
      target: { value: "sales order" },
    });
    expect(onQueryChange).toHaveBeenCalledWith("sales order");
  });

  it("clicking a filter chip calls onFilterChange with that filter", () => {
    const onFilterChange = vi.fn();
    render(<CeligoScriptsList {...baseProps({ onFilterChange })} />);
    fireEvent.click(screen.getByRole("button", { name: "Diverged" }));
    expect(onFilterChange).toHaveBeenCalledWith("diverged");
  });

  it("picking an integration in the select calls onIntegrationChange with its id", () => {
    const onIntegrationChange = vi.fn();
    render(<CeligoScriptsList {...baseProps({ onIntegrationChange })} />);
    fireEvent.change(screen.getByLabelText("Filter by integration"), { target: { value: "int-2" } });
    expect(onIntegrationChange).toHaveBeenCalledWith("int-2");
    expect(screen.getByText("Backfills")).toBeInTheDocument();
  });

  it('shows "Showing X of Y families" only once the visible set is filtered down', () => {
    const { rerender } = render(<CeligoScriptsList {...baseProps()} />);
    expect(screen.queryByText(/showing \d+ of \d+ families/i)).not.toBeInTheDocument();
    rerender(<CeligoScriptsList {...baseProps({ kind: "hook" })} />);
    expect(screen.getByText("Showing 2 of 4 families")).toBeInTheDocument();
  });

  it("filtering to nothing renders a no-match message, not empty rows silently", () => {
    render(<CeligoScriptsList {...baseProps({ q: "no such script anywhere" })} />);
    expect(screen.getByText(/no families match/i)).toBeInTheDocument();
  });

  it("ArrowDown/ArrowUp move which row is highlighted, Enter commits it via onSelect", () => {
    const onSelect = vi.fn();
    render(<CeligoScriptsList {...baseProps({ onSelect })} />);
    const rows = screen.getByTestId("celigo-scripts-rows");
    fireEvent.keyDown(rows, { key: "ArrowDown" });
    expect(screen.getByRole("button", { name: /ns_sales_order_premap/ })).toHaveAttribute("aria-pressed", "true");
    fireEvent.keyDown(rows, { key: "ArrowDown" });
    expect(screen.getByRole("button", { name: /sales_order_script_v2/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /ns_sales_order_premap/ })).toHaveAttribute("aria-pressed", "false");
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.keyDown(rows, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith("fam-hook-b");
    fireEvent.keyDown(rows, { key: "ArrowUp" });
    expect(screen.getByRole("button", { name: /ns_sales_order_premap/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("clicking a row selects it directly", () => {
    const onSelect = vi.fn();
    render(<CeligoScriptsList {...baseProps({ onSelect })} />);
    fireEvent.click(screen.getByRole("button", { name: /service_stock_sync_transform/ }));
    expect(onSelect).toHaveBeenCalledWith("fam-transform-a");
  });

  it("marks the row matching selectedKey as pressed on initial render", () => {
    render(<CeligoScriptsList {...baseProps({ selectedKey: "fam-transform-a" })} />);
    expect(screen.getByRole("button", { name: /service_stock_sync_transform/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
