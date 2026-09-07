import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { resolved, pending, errored } from "../../__tests__/query-fixtures";
import type {
  CeligoScriptFamilyDetail,
  CeligoScriptFamilyMember,
  CeligoScriptFamilySite,
  CeligoScriptFamilyVersion,
} from "@/hooks/use-celigo-flows";
import { N2_SHIELD_TEXT } from "../../shared";

// Task 5 — the Scripts view's detail pane: header facts, the versions
// strip, the source/compare panes, and the where-used table (spec §3.3).
// `useCeligoScriptFamily` and `useCeligoRoute` are mocked (this file owns
// only the detail pane's OWN behaviour), and `DiffViewer` is stubbed to a
// prop-capturing div so compare-mode assertions never touch real Monaco.

const mocks = vi.hoisted(() => ({ family: vi.fn() }));
vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoScriptFamily: (dedupKey: string | null) => mocks.family(dedupKey),
}));

const routeMocks = vi.hoisted(() => ({
  go: { flow: vi.fn(), scripts: vi.fn() },
  copyId: null as string | null,
  compare: null as { left: string; right: string } | null,
  scriptsFilter: "all" as string,
  scriptsKind: null as string | null,
  q: "" as string,
  scriptsIntegrationId: null as string | null,
}));
vi.mock("../../celigo-route", () => ({
  useCeligoRoute: () => ({
    copyId: routeMocks.copyId,
    compare: routeMocks.compare,
    scriptsFilter: routeMocks.scriptsFilter,
    scriptsKind: routeMocks.scriptsKind,
    q: routeMocks.q,
    scriptsIntegrationId: routeMocks.scriptsIntegrationId,
    go: routeMocks.go,
  }),
}));

const diffMocks = vi.hoisted(() => ({ render: vi.fn() }));
vi.mock("@/components/workspace/diff-viewer", () => ({
  DiffViewer: (props: Record<string, unknown>) => {
    diffMocks.render(props);
    return <div data-testid="scripts-diff-viewer" />;
  },
}));

import { CeligoScriptsDetail, defaultComparePair, versionForCopy } from "../celigo-scripts-detail";

function member(overrides: Partial<CeligoScriptFamilyMember>): CeligoScriptFamilyMember {
  return {
    script_id: "script-default",
    celigo_id: "celigo-default",
    name: "ns_sales_order_premap",
    is_original: false,
    version_letter: "A",
    content_hash: "hash-a",
    size_bytes: 100,
    celigo_last_modified: "2025-09-03T00:00:00Z",
    sites_count: 1,
    flows_count: 1,
    content: "function preMap(o){return o.data}",
    ...overrides,
  };
}

function version(overrides: Partial<CeligoScriptFamilyVersion>): CeligoScriptFamilyVersion {
  return {
    letter: "A",
    content_hash: "hash-a",
    copies_count: 1,
    sites_count: 1,
    first_seen: "2025-09-03T00:00:00Z",
    size_bytes: 2355,
    holds_original: false,
    ...overrides,
  };
}

function site(overrides: Partial<CeligoScriptFamilySite>): CeligoScriptFamilySite {
  return {
    attachment_id: "att-default",
    script_id: "script-default",
    script_celigo_id: "celigo-default",
    version_letter: "A",
    integration_id: "int-1",
    integration_name: "Solidus + NetSuite",
    flow_id: "flow-1",
    flow_name: "New Sales Order to NetSuite",
    flow_disabled: false,
    flow_step_id: "step-1",
    step_reference_name: "Add New Sales Order (Framework BV)",
    step_role: "processor",
    step_adaptor_type: "NetSuiteDistributedImport",
    step_record_type: "salesorder",
    step_operation: "add",
    json_path: "66738c3d…e46.hooks.preMap",
    function_name: "preMap",
    site_type: "hook",
    open_error_count: 0,
    errors_checked_at: "2026-09-06T11:00:00Z",
    ...overrides,
  };
}

// --- Family A: models mock State One (ns_sales_order_premap). ------------

const MEMBER_A = member({
  script_id: "member-a",
  celigo_id: "celigo-a",
  version_letter: "A",
  content_hash: "hash-a",
  content: "function preMap(o){return o.data} // version A",
  celigo_last_modified: "2025-09-03T00:00:00Z",
  is_original: false,
});
const MEMBER_B = member({
  script_id: "member-b",
  celigo_id: "celigo-b",
  version_letter: "B",
  content_hash: "hash-b",
  content: "function preMap(o){return o.data} // version B",
  celigo_last_modified: "2026-03-24T00:00:00Z",
  is_original: false,
});
const MEMBER_C_ORIGINAL = member({
  script_id: "member-c",
  celigo_id: "fam-a", // celigo_id === dedup_key -> the original
  version_letter: "C",
  content_hash: "hash-c",
  content: "function preMap(o){return o.data} // version C, the original",
  celigo_last_modified: "2026-06-30T00:00:00Z",
  is_original: true,
});

const VERSIONS_A: CeligoScriptFamilyVersion[] = [
  version({ letter: "A", content_hash: "hash-a", copies_count: 1, sites_count: 2, first_seen: "2025-09-03T00:00:00Z", size_bytes: 2355, holds_original: false }),
  version({ letter: "B", content_hash: "hash-b", copies_count: 3, sites_count: 6, first_seen: "2025-10-07T00:00:00Z", size_bytes: 2150, holds_original: false }),
  version({ letter: "C", content_hash: "hash-c", copies_count: 3, sites_count: 8, first_seen: "2026-04-23T00:00:00Z", size_bytes: 2458, holds_original: true }),
];

const SITES_A: CeligoScriptFamilySite[] = [
  site({ attachment_id: "att-1", script_id: "member-c", version_letter: "C", flow_name: "New Sales Order to NetSuite", open_error_count: 1, errors_checked_at: "2026-09-06T11:00:00Z" }),
  site({ attachment_id: "att-2", script_id: "member-c", version_letter: "C", flow_name: "Update Pre-Orders", open_error_count: 0 }),
  site({ attachment_id: "att-3", script_id: "member-b", version_letter: "B", flow_name: "Manual Update Pre-Orders", errors_checked_at: null, open_error_count: 0 }),
  site({ attachment_id: "att-4", script_id: "member-b", version_letter: "B", flow_name: "Backfill Sales Order", flow_disabled: true }),
  site({ attachment_id: "att-5", script_id: "member-c", version_letter: "C", flow_name: "Router-level site", flow_step_id: null, step_reference_name: null, step_role: null, step_adaptor_type: null, open_error_count: null, site_type: "router" }),
  site({ attachment_id: "att-6", script_id: "member-c", version_letter: "C", flow_name: "Flow six" }),
  site({ attachment_id: "att-7", script_id: "member-c", version_letter: "C", flow_name: "Flow seven" }),
  site({ attachment_id: "att-8", script_id: "member-c", version_letter: "C", flow_name: "Flow eight" }),
  site({ attachment_id: "att-9", script_id: "member-c", version_letter: "C", flow_name: "Flow nine" }),
];

const FAMILY_A: CeligoScriptFamilyDetail = {
  summary: {
    dedup_key: "fam-a",
    name: "ns_sales_order_premap",
    kind: "hook",
    function_name: "preMap",
    copies_count: 7,
    versions_count: 3,
    content_diverged: true,
    original_present: true,
    sites_count: SITES_A.length,
    flows_count: 8,
    integrations_count: 2,
    integration_ids: ["int-1", "int-2"],
    flow_names: ["New Sales Order to NetSuite"],
    sites_with_open_errors: 1,
    sites_unchecked: 1,
    first_modified: "2025-09-03T00:00:00Z",
    last_modified: "2026-06-30T00:00:00Z",
    max_size_bytes: 2458,
    other_families_with_name: 0,
  },
  members: [MEMBER_A, MEMBER_B, MEMBER_C_ORIGINAL],
  versions: VERSIONS_A,
  sites: SITES_A,
};

// --- Family B: models mock State Two (FW Sales Order Hook) — 4 diverged
// versions, three spare (0 sites), one original that runs. Used for the
// versions-strip "spare copy" case and every compare-mode assertion. -----

const VERSIONS_B: CeligoScriptFamilyVersion[] = [
  version({ letter: "A", content_hash: "hash-a", copies_count: 1, sites_count: 0, first_seen: "2023-01-24T00:00:00Z", size_bytes: 4700, holds_original: false }),
  version({ letter: "B", content_hash: "hash-b", copies_count: 1, sites_count: 0, first_seen: "2023-01-24T00:00:00Z", size_bytes: 7800, holds_original: false }),
  version({ letter: "C", content_hash: "hash-c", copies_count: 1, sites_count: 0, first_seen: "2023-07-19T00:00:00Z", size_bytes: 14100, holds_original: false }),
  version({ letter: "D", content_hash: "hash-d", copies_count: 1, sites_count: 5, first_seen: "2024-05-22T00:00:00Z", size_bytes: 17600, holds_original: true }),
];

const FAMILY_B: CeligoScriptFamilyDetail = {
  summary: {
    dedup_key: "fam-b",
    name: "FW Sales Order Hook",
    kind: "hook",
    function_name: "preSavePage",
    copies_count: 4,
    versions_count: 4,
    content_diverged: true,
    original_present: true,
    sites_count: 5,
    flows_count: 5,
    integrations_count: 3,
    integration_ids: ["int-1"],
    flow_names: ["Flow A"],
    sites_with_open_errors: 0,
    sites_unchecked: 0,
    first_modified: "2023-01-24T00:00:00Z",
    last_modified: "2024-05-22T00:00:00Z",
    max_size_bytes: 17600,
    other_families_with_name: 0,
  },
  members: [
    member({ script_id: "b-member-a", celigo_id: "b-a", version_letter: "A", content: "// A", is_original: false }),
    member({ script_id: "b-member-d", celigo_id: "fam-b", version_letter: "D", content: "// D original", is_original: true }),
  ],
  versions: VERSIONS_B,
  sites: [site({ attachment_id: "b-att-1", script_id: "b-member-d", version_letter: "D" })],
};

// --- Family C: models mock State Three (Framework 945 v2) — unattached,
// no original in production, 2 versions, no sites at all. -----------------

const FAMILY_C: CeligoScriptFamilyDetail = {
  summary: {
    dedup_key: "fam-c",
    name: "Framework 945 v2",
    kind: "unattached",
    function_name: "postResponseMap",
    copies_count: 2,
    versions_count: 2,
    content_diverged: true,
    original_present: false,
    sites_count: 0,
    flows_count: 0,
    integrations_count: 0,
    integration_ids: [],
    flow_names: [],
    sites_with_open_errors: 0,
    sites_unchecked: 0,
    first_modified: "2023-01-24T00:00:00Z",
    last_modified: "2023-01-24T00:00:00Z",
    max_size_bytes: 3400,
    other_families_with_name: 0,
  },
  members: [
    member({ script_id: "c-member-a", celigo_id: "c-a", version_letter: "A", content: "function postResponseMap(o){return o.postResponseMapData}", is_original: false, sites_count: 0 }),
    member({ script_id: "c-member-b", celigo_id: "c-b", version_letter: "B", content: "function postResponseMap(o){return null}", is_original: false, sites_count: 0 }),
  ],
  versions: [
    version({ letter: "A", content_hash: "c-hash-a", copies_count: 1, sites_count: 0, first_seen: "2023-01-24T00:00:00Z", size_bytes: 3400, holds_original: false }),
    version({ letter: "B", content_hash: "c-hash-b", copies_count: 1, sites_count: 0, first_seen: "2023-01-24T00:00:00Z", size_bytes: 3300, holds_original: false }),
  ],
  sites: [],
};

function wrap(dedupKey: string) {
  return render(<CeligoScriptsDetail dedupKey={dedupKey} />);
}

beforeEach(() => {
  routeMocks.go.flow.mockReset();
  routeMocks.go.scripts.mockReset();
  diffMocks.render.mockReset();
  Object.assign(routeMocks, {
    copyId: null,
    compare: null,
    scriptsFilter: "all",
    scriptsKind: null,
    q: "",
    scriptsIntegrationId: null,
  });
  mocks.family.mockReset();
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});

describe("CeligoScriptsDetail — query states", () => {
  it("pending renders a loading indicator, never empty content", () => {
    mocks.family.mockReturnValue(pending());
    wrap("fam-a");
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it("error renders a retry notice", () => {
    const refetch = vi.fn();
    mocks.family.mockReturnValue(errored(refetch));
    wrap("fam-a");
    expect(screen.getByText(/couldn.?t load/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });
});

describe("CeligoScriptsDetail — header facts", () => {
  it("renders name, copies, versions, function, size, and modified date", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    const header = screen.getByTestId("scripts-detail-header");
    expect(within(header).getByText("ns_sales_order_premap")).toBeInTheDocument();
    expect(within(header).getByText(/7 copies/)).toBeInTheDocument();
    expect(within(header).getByText(/3 versions/)).toBeInTheDocument();
    expect(within(header).getByText("preMap")).toBeInTheDocument();
    expect(within(header).getByText(/2\.4 KB/)).toBeInTheDocument();
    expect(within(header).getByText(/30 Jun 2026/)).toBeInTheDocument();
  });
});

describe("CeligoScriptsDetail — versions strip", () => {
  it("shows letter, copies, sites, first seen, size, and an original mark on the holding version", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    const cardC = screen.getByRole("button", { name: /version c/i });
    expect(within(cardC).getByText(/3 copies/)).toBeInTheDocument();
    expect(within(cardC).getByText(/8 sites/)).toBeInTheDocument();
    expect(within(cardC).getByText(/23 Apr 2026/)).toBeInTheDocument();
    expect(within(cardC).getByText(/2\.4 KB/)).toBeInTheDocument();
    expect(within(cardC).getByText(/original/i)).toBeInTheDocument();
  });

  it("marks a version with zero sites as a spare copy", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_B));
    wrap("fam-b");
    const cardA = screen.getByRole("button", { name: /version a/i });
    expect(within(cardA).getByText(/spare copy/i)).toBeInTheDocument();
  });
});

describe("CeligoScriptsDetail — copy= arrival", () => {
  it("arriving with copy=<script_id> selects that member's version", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    Object.assign(routeMocks, { copyId: "member-b" });
    wrap("fam-a");
    expect(screen.getByRole("button", { name: /version b/i })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/showing version b/i)).toBeInTheDocument();
  });
});

describe("CeligoScriptsDetail — source bar / original vs clone", () => {
  it("names the original copy when the shown version holds it", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    expect(screen.getByText(/showing version c.*the original copy/i)).toBeInTheDocument();
  });

  it("names a clone when the shown version does not hold the original", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    fireEvent.click(screen.getByRole("button", { name: /version b/i }));
    expect(screen.getByText(/showing version b.*a clone/i)).toBeInTheDocument();
  });

  it("states neither original nor clone when no original is in production", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_C));
    wrap("fam-c");
    expect(screen.getByText(/^showing version a$/i)).toBeInTheDocument();
  });

  it("renders the N2 shield verbatim", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    expect(screen.getByText(N2_SHIELD_TEXT)).toBeInTheDocument();
  });
});

describe("CeligoScriptsDetail — compare mode", () => {
  it("disables Compare versions when the family has only one version", () => {
    mocks.family.mockReturnValue(
      resolved({
        ...FAMILY_C,
        versions: [FAMILY_C.versions[0]],
        summary: { ...FAMILY_C.summary, versions_count: 1, content_diverged: false },
      }),
    );
    wrap("fam-c");
    expect(screen.getByRole("button", { name: /compare versions/i })).toBeDisabled();
  });

  it("shows the default pair (oldest -> the original's version) when entering compare mode", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    fireEvent.click(screen.getByRole("button", { name: /compare versions/i }));
    expect(routeMocks.go.scripts).toHaveBeenCalledWith(
      expect.objectContaining({ family: "fam-a", compare: { left: "A", right: "C" } }),
    );
  });

  it("renders the two compared versions' content in the diff viewer when compare= is on the URL", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    Object.assign(routeMocks, { compare: { left: "A", right: "C" } });
    wrap("fam-a");
    expect(diffMocks.render).toHaveBeenCalledWith(
      expect.objectContaining({
        original: MEMBER_A.content,
        modified: MEMBER_C_ORIGINAL.content,
        sideBySide: true,
      }),
    );
  });

  it("changing a version picker updates the pair and the URL", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    Object.assign(routeMocks, { compare: { left: "A", right: "C" } });
    wrap("fam-a");
    fireEvent.change(screen.getByLabelText(/compare.*left/i), { target: { value: "B" } });
    expect(routeMocks.go.scripts).toHaveBeenCalledWith(
      expect.objectContaining({ compare: { left: "B", right: "C" } }),
    );
  });

  it("Side by side / Inline toggles the sideBySide prop passed to DiffViewer", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    Object.assign(routeMocks, { compare: { left: "A", right: "C" } });
    wrap("fam-a");
    fireEvent.click(screen.getByRole("button", { name: /^inline$/i }));
    expect(diffMocks.render).toHaveBeenLastCalledWith(expect.objectContaining({ sideBySide: false }));
    fireEvent.click(screen.getByRole("button", { name: /side by side/i }));
    expect(diffMocks.render).toHaveBeenLastCalledWith(expect.objectContaining({ sideBySide: true }));
  });
});

describe("CeligoScriptsDetail — where used", () => {
  it("shows a paused pill for a disabled flow", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    const row = screen.getByText("Backfill Sales Order").closest("tr")!;
    expect(within(row).getByText(/paused/i)).toBeInTheDocument();
  });

  it("shows the step reference name with a role · adaptor sub-line", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    const row = screen.getByText("New Sales Order to NetSuite").closest("tr")!;
    expect(within(row).getByText("Add New Sales Order (Framework BV)")).toBeInTheDocument();
    expect(within(row).getByText(/NetSuiteDistributedImport/)).toBeInTheDocument();
  });

  it("renders json_path as mono text", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    expect(screen.getAllByText("66738c3d…e46.hooks.preMap")[0]).toHaveClass("font-mono");
  });

  it("formats the copy column as letter · original / letter · clone <date>", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    const originalRow = screen.getByText("New Sales Order to NetSuite").closest("tr")!;
    expect(within(originalRow).getByText(/c\s*·\s*original/i)).toBeInTheDocument();
    const cloneRow = screen.getByText("Manual Update Pre-Orders").closest("tr")!;
    expect(within(cloneRow).getByText(/b\s*·\s*clone\s*24 Mar 2026/i)).toBeInTheDocument();
  });

  it("shows the errors cell states: open (crit), zero, not checked, and — for router sites", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    const openRow = screen.getByText("New Sales Order to NetSuite").closest("tr")!;
    expect(within(openRow).getByText(/1 open/)).toBeInTheDocument();
    const zeroRow = screen.getByText("Update Pre-Orders").closest("tr")!;
    expect(within(zeroRow).getByText("0")).toBeInTheDocument();
    const uncheckedRow = screen.getByText("Manual Update Pre-Orders").closest("tr")!;
    expect(within(uncheckedRow).getByText(/not checked/i)).toBeInTheDocument();
    const routerRow = screen.getByText("Router-level site").closest("tr")!;
    expect(within(routerRow).getByText("—")).toBeInTheDocument();
  });

  it("the ↗ control navigates with the site's flow and integration", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    const row = screen.getByText("New Sales Order to NetSuite").closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /open.*flow map/i }));
    expect(routeMocks.go.flow).toHaveBeenCalledWith("flow-1", "int-1");
  });

  it("shows 8 rows by default, with a Show all control for the rest", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    expect(screen.getAllByRole("row")).toHaveLength(1 + 8); // header + 8 body rows
    expect(screen.getByText(/8 of 9 shown/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /show all 9/i }));
    expect(screen.getAllByRole("row")).toHaveLength(1 + 9);
  });
});

describe("CeligoScriptsDetail — Open in flow map / Copy source", () => {
  it("disables Open in flow map for an unattached family", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_C));
    wrap("fam-c");
    expect(screen.getByRole("button", { name: /open in flow map/i })).toBeDisabled();
  });

  it("copy source writes the shown version's content to the clipboard", () => {
    mocks.family.mockReturnValue(resolved(FAMILY_A));
    wrap("fam-a");
    fireEvent.click(screen.getByRole("button", { name: /copy source/i }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(MEMBER_C_ORIGINAL.content);
  });
});

describe("pure helpers", () => {
  it("defaultComparePair pairs the oldest version with the one holding the original", () => {
    expect(defaultComparePair(VERSIONS_A)).toEqual({ left: "A", right: "C" });
  });

  it("defaultComparePair falls back to oldest -> newest when no version holds the original", () => {
    const versions = [
      version({ letter: "A", holds_original: false }),
      version({ letter: "B", holds_original: false }),
    ];
    expect(defaultComparePair(versions)).toEqual({ left: "A", right: "B" });
  });

  it("defaultComparePair returns null with fewer than two versions", () => {
    expect(defaultComparePair([VERSIONS_A[0]])).toBeNull();
  });

  it("versionForCopy resolves a member's version letter by script_id", () => {
    expect(versionForCopy(FAMILY_A.members, "member-b")).toBe("B");
    expect(versionForCopy(FAMILY_A.members, null)).toBeNull();
    expect(versionForCopy(FAMILY_A.members, "not-a-real-id")).toBeNull();
  });
});
