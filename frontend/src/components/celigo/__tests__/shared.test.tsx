import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";
import {
  formatRelativeTime,
  adaptorFamily,
  fallbackStepTitle,
  deriveFlowSummary,
  ErrorPill,
  Pill,
  SchedulePill,
  Medallions,
  N2_SHIELD_TEXT,
} from "../shared";
import type { CeligoFlowDetail, CeligoFlowStep } from "@/hooks/use-celigo-flows";

afterEach(() => {
  vi.useRealTimers();
});

describe("formatRelativeTime — relative to a passed-in `now`, never the wall clock", () => {
  const now = new Date("2026-09-02T18:12:00Z");
  it("minutes", () => {
    expect(formatRelativeTime("2026-09-02T17:51:00Z", now)).toBe("21 min ago");
  });
  it("hours", () => {
    expect(formatRelativeTime("2026-09-02T15:12:00Z", now)).toBe("3 h ago");
  });
  it("days", () => {
    expect(formatRelativeTime("2026-08-27T18:12:00Z", now)).toBe("6 days ago");
  });
  it("months", () => {
    expect(formatRelativeTime("2025-04-02T18:12:00Z", now)).toBe("17 months ago");
  });
  it("years", () => {
    expect(formatRelativeTime("2024-09-02T18:12:00Z", now)).toBe("2 years ago");
  });
  it("null is an em dash, never a fabricated value", () => {
    expect(formatRelativeTime(null, now)).toBe("—");
  });
});

describe("adaptorFamily — Celigo's adaptor type strings grouped into an app family", () => {
  it("recognises every family seen live", () => {
    expect(adaptorFamily("NetSuiteDistributedImport")).toBe("NetSuite");
    expect(adaptorFamily("AS2Import")).toBe("AS2");
    expect(adaptorFamily("RDBMSExport")).toBe("RDBMS");
    expect(adaptorFamily("RESTImport")).toBe("REST");
    expect(adaptorFamily("HTTPExport")).toBe("HTTP");
  });
  it("null in, null out", () => {
    expect(adaptorFamily(null)).toBeNull();
  });
});

const stepBase: CeligoFlowStep = {
  id: "s",
  celigo_id: "c",
  role: "processor",
  kind: "destination",
  router_id: null,
  branch_id: null,
  branch_key: "$root",
  sequence: 0,
  adaptor_type: null,
  connection_celigo_id: null,
  filter_json: null,
  mapping_json: null,
  proceed_on_failure: null,
  skip_retries: null,
  attachments: [],
  reference_name: null,
  record_type: null,
  operation: null,
  search_id: null,
  error_count: 0,
};

describe("fallbackStepTitle — the honest fallback, never an invented name", () => {
  it("NetSuite destination: a confident fact, not marked unsynced", () => {
    expect(
      fallbackStepTitle({ ...stepBase, kind: "destination", adaptor_type: "NetSuiteDistributedImport", record_type: "salesorder", operation: "add" }),
    ).toEqual({ text: "add salesorder", unsynced: false });
  });
  it("NetSuite lookup by saved search: still marked unsynced (the name, not the search id, is missing)", () => {
    expect(
      fallbackStepTitle({ ...stepBase, kind: "lookup", adaptor_type: "NetSuiteExport", record_type: "customer", search_id: "5090" }),
    ).toEqual({ text: "lookup customer · search 5090", unsynced: true });
  });
  it("HTTP source: name not synced, nothing else to say", () => {
    expect(fallbackStepTitle({ ...stepBase, kind: "source", adaptor_type: "HTTPExport" })).toEqual({
      text: "HTTP export · name not synced",
      unsynced: true,
    });
  });
  it("HTTP lookup: name not synced", () => {
    expect(fallbackStepTitle({ ...stepBase, kind: "lookup", adaptor_type: "HTTPExport" })).toEqual({
      text: "HTTP lookup · name not synced",
      unsynced: true,
    });
  });

  // Codex fix wave, item 6. `${family ?? "HTTP"}` picked a real, specific
  // app family out of thin air whenever the adaptor was not synced — the one
  // thing this whole function exists to avoid. HTTP is a plausible guess for
  // a Celigo flow, which is exactly what makes it dangerous: it reads as a
  // synced fact.
  it("an unsynced adaptor names the KIND, never a family nobody supplied", () => {
    expect(fallbackStepTitle({ ...stepBase, kind: "destination", adaptor_type: null })).toEqual({
      text: "Destination · adaptor not synced",
      unsynced: true,
    });
    expect(fallbackStepTitle({ ...stepBase, kind: "source", adaptor_type: null })).toEqual({
      text: "Source · adaptor not synced",
      unsynced: true,
    });
    expect(fallbackStepTitle({ ...stepBase, kind: "lookup", adaptor_type: "" })).toEqual({
      text: "Lookup · adaptor not synced",
      unsynced: true,
    });
  });

  it("an unrecognised adaptor string is still not HTTP", () => {
    const result = fallbackStepTitle({ ...stepBase, kind: "destination", adaptor_type: "WombatImport" });
    expect(result.text).toBe("Destination · adaptor not synced");
    expect(result.text).not.toContain("HTTP");
  });
});

describe("deriveFlowSummary — computed off the flow's own steps/routers, never a hardcoded name", () => {
  function step(over: Partial<CeligoFlowStep>): CeligoFlowStep {
    return { ...stepBase, id: over.celigo_id as string, ...over };
  }

  it("source + 1 lookup + a 2-branch router describes the routing and the per-branch verbs", () => {
    const detail: CeligoFlowDetail = {
      id: "f1",
      integration_id: "i1",
      celigo_id: "flow_chain",
      name: "New Sales Order to NetSuite - Multi-Subsidiary",
      disabled: false,
      schedule: "? 5,20,35,50 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23 ? * *",
      timezone: "America/Los_Angeles",
      last_executed_at: "2026-09-02T17:51:00Z",
      source_id: null,
      ai_description_summary: null,
      ai_description_detailed: null,
      celigo_last_modified: "2026-09-02T00:00:00Z",
      unassigned_attachments: [],
      celigo_open_error_count: 0,
      last_error_at: null,
      error_count: 0,
      signature_count: 0,
      routers: [
        {
          id: "r2",
          name: "",
          route_records_to: "first_matching_branch",
          route_records_using: "input_filters",
          has_script_slot: false,
          branches: [
            { id: "bIntl", name: "Framework Intl", rule_count: 1, next_router_id: null, order: 0, declared_step_count: 4 },
            { id: "bInc", name: "Framework Inc", rule_count: 1, next_router_id: null, order: 1, declared_step_count: 4 },
          ],
        },
      ],
      steps: [
        step({ celigo_id: "src", kind: "source", adaptor_type: "HTTPExport", sequence: 0 }),
        step({ celigo_id: "lkp", kind: "lookup", adaptor_type: "HTTPExport", sequence: 1 }),
        step({
          celigo_id: "cust_lkp_bIntl",
          kind: "lookup",
          adaptor_type: "NetSuiteExport",
          router_id: "r2",
          branch_id: "bIntl",
          sequence: 0,
          record_type: "customer",
          search_id: "5090",
        }),
        step({
          celigo_id: "cust_add_bIntl",
          kind: "destination",
          adaptor_type: "NetSuiteDistributedImport",
          router_id: "r2",
          branch_id: "bIntl",
          sequence: 1,
          record_type: "customer",
          operation: "add",
        }),
        step({
          celigo_id: "cust_upd_bIntl",
          kind: "destination",
          adaptor_type: "NetSuiteDistributedImport",
          router_id: "r2",
          branch_id: "bIntl",
          sequence: 2,
          record_type: "customer",
          operation: "update",
        }),
        step({
          celigo_id: "so_add_bIntl",
          kind: "destination",
          adaptor_type: "NetSuiteDistributedImport",
          router_id: "r2",
          branch_id: "bIntl",
          sequence: 3,
          record_type: "salesorder",
          operation: "add",
        }),
      ],
    };
    const summary = deriveFlowSummary(detail);
    expect(summary).toContain("routes on");
    expect(summary).toContain("adds the sales order");
  });

  it("falls back to a bare shape count when there's no source step to anchor on", () => {
    const detail: CeligoFlowDetail = {
      id: "f2",
      integration_id: "i1",
      celigo_id: "flow_empty",
      name: "Empty",
      disabled: false,
      schedule: null,
      timezone: null,
      last_executed_at: null,
      source_id: null,
      ai_description_summary: null,
      ai_description_detailed: null,
      celigo_last_modified: null,
      unassigned_attachments: [],
      celigo_open_error_count: null,
      last_error_at: null,
      error_count: 0,
      signature_count: 0,
      routers: [],
      steps: [],
    };
    expect(deriveFlowSummary(detail)).toBe("0 steps · 0 routers");
  });

  // Codex fix wave, item 11.
  function bareDetail(over: Partial<CeligoFlowDetail>): CeligoFlowDetail {
    return {
      id: "f",
      integration_id: "i1",
      celigo_id: "flow",
      name: "Flow",
      disabled: false,
      schedule: null,
      timezone: null,
      last_executed_at: null,
      source_id: null,
      ai_description_summary: null,
      ai_description_detailed: null,
      celigo_last_modified: null,
      unassigned_attachments: [],
      celigo_open_error_count: null,
      last_error_at: null,
      error_count: 0,
      signature_count: 0,
      routers: [],
      steps: [],
      ...over,
    };
  }

  it("(a) names EVERY source, not just the first one it happened to find", () => {
    const detail = bareDetail({
      steps: [
        { ...stepBase, id: "s1", kind: "source", adaptor_type: "NetSuiteExport", sequence: 0 },
        { ...stepBase, id: "s2", kind: "source", adaptor_type: "FTPExport", sequence: 1 },
      ],
    });
    expect(deriveFlowSummary(detail)).toContain("from NetSuite and FTP");
  });

  it("(a) an unsynced adaptor on a source is said out loud, not silently dropped", () => {
    const detail = bareDetail({
      steps: [
        { ...stepBase, id: "s1", kind: "source", adaptor_type: "NetSuiteExport", sequence: 0 },
        { ...stepBase, id: "s2", kind: "source", adaptor_type: null, sequence: 1 },
      ],
    });
    expect(deriveFlowSummary(detail)).toContain("from NetSuite and an unsynced adaptor");
  });

  it("(b) makes no per-branch claim when the router's branches carry no ids", () => {
    const detail = bareDetail({
      routers: [
        {
          id: "r1",
          name: null,
          route_records_to: "branches",
          route_records_using: "filters",
          has_script_slot: false,
          branches: [
            { id: null, name: null, rule_count: 0, next_router_id: null, order: 0, declared_step_count: 1 },
            { id: null, name: null, rule_count: 0, next_router_id: null, order: 1, declared_step_count: 1 },
          ],
        },
      ],
      steps: [
        { ...stepBase, id: "src", kind: "source", adaptor_type: "HTTPExport", sequence: 0 },
        {
          ...stepBase,
          id: "d1",
          kind: "destination",
          adaptor_type: "NetSuiteDistributedImport",
          router_id: "r1",
          branch_id: null,
          record_type: "salesorder",
          operation: "add",
          sequence: 1,
        },
      ],
    });
    const summary = deriveFlowSummary(detail);
    expect(summary).toContain("routes to 2 branches (branch ids not synced)");
    expect(summary).not.toContain("per branch:");
  });

  it("(b) still describes the branch when the ids ARE there", () => {
    const detail = bareDetail({
      routers: [
        {
          id: "r1",
          name: null,
          route_records_to: "branches",
          route_records_using: "filters",
          has_script_slot: false,
          branches: [
            { id: "b1", name: "One", rule_count: 1, next_router_id: null, order: 0, declared_step_count: 1 },
            { id: "b2", name: "Two", rule_count: 1, next_router_id: null, order: 1, declared_step_count: 1 },
          ],
        },
      ],
      steps: [
        { ...stepBase, id: "src", kind: "source", adaptor_type: "HTTPExport", sequence: 0 },
        {
          ...stepBase,
          id: "d1",
          kind: "destination",
          adaptor_type: "NetSuiteDistributedImport",
          router_id: "r1",
          branch_id: "b1",
          record_type: "salesorder",
          operation: "add",
          sequence: 1,
        },
      ],
    });
    expect(deriveFlowSummary(detail)).toContain("per branch: adds the sales order");
  });
});

describe("ErrorPill — a zero is a claim with a timestamp, not a decoration", () => {
  it("clean: leads with the error fact and the time it was checked", () => {
    // ErrorPill's `checkedAt` -> formatRelativeTime defaults `now` to the
    // real clock, so the system clock is frozen to make the rendered text
    // deterministic regardless of when the test runs.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-02T18:12:00Z"));
    render(<ErrorPill count={0} checkedAt="2026-09-02T18:08:00Z" />);
    expect(screen.getByText("0 open errors")).toBeInTheDocument();
    expect(screen.getByText(/checked 4 min ago/)).toBeInTheDocument();
  });
  it("not clean: root cause count leads, raw count follows", () => {
    render(<ErrorPill count={10} signatureCount={1} checkedAt={null} />);
    expect(screen.getByText(/10 open/)).toBeInTheDocument();
    expect(screen.getByText(/1 root cause/)).toBeInTheDocument();
  });
});

describe("Pill / SchedulePill / Medallions — small presentational pieces", () => {
  it("Pill renders its children with a dot when asked", () => {
    const { container } = render(
      <Pill tone="ok" dot="solid">
        on time
      </Pill>,
    );
    expect(screen.getByText("on time")).toBeInTheDocument();
    expect(container.querySelector("span > span")).not.toBeNull();
  });

  it("SchedulePill: stalled carries the missed-run count and keeps the '?'", () => {
    render(<SchedulePill stall={{ state: "stalled", missedRuns: 12, intervalMinutes: 15 }} parsed={{ kind: "on_demand" }} />);
    expect(screen.getByText(/stalled\?/)).toBeInTheDocument();
    expect(screen.getByText(/12 runs missed/)).toBeInTheDocument();
  });

  it("SchedulePill: on time", () => {
    render(<SchedulePill stall={{ state: "on_time", intervalMinutes: 15 }} parsed={{ kind: "on_demand" }} />);
    expect(screen.getByText("on time")).toBeInTheDocument();
  });

  it("Medallions renders one badge per family, in the order given", () => {
    render(<Medallions families={["HTTP", "NetSuite", "RDBMS"]} />);
    expect(screen.getByText("HTTP")).toBeInTheDocument();
    expect(screen.getByText("NS")).toBeInTheDocument();
    expect(screen.getByText("DB")).toBeInTheDocument();
  });
});

describe("N2_SHIELD_TEXT — the one definition of the mandated banner string", () => {
  // Gate fix wave, item 12. This string is mandated verbatim by Global
  // Constraints and was previously hand-copied into both surfaces that show
  // customer JavaScript. It now lives here alone; both import it, so the
  // wording is pinned in exactly one place -- this assertion.
  it("is the exact mandated wording", () => {
    expect(N2_SHIELD_TEXT).toBe(
      "Customer-authored JavaScript, shown to you only. Never run here, never sent to the assistant.",
    );
  });
});
