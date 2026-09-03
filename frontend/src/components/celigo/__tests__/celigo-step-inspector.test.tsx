import { render, screen, within, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import type {
  CeligoAttachment,
  CeligoFlowDetail,
  CeligoFlowErrorGroup,
  CeligoFlowStep,
  CeligoRouter,
} from "@/hooks/use-celigo-flows";
import { resolved, pending, errored } from "./query-fixtures";

// Task 16 — the real inspector (mockup screen 3's `.insp`): Facts / Filter /
// Mapping / Scripts / Errors tabs for the selected step, replacing the Task
// 14 stub. Mocks the hooks module the same way Tasks 10/12/14 do.
// `useCeligoScript` is mocked too (even though `CeligoStepInspector` never
// calls it) purely as an N2 tripwire -- the "no script content" test below
// proves the rendered DOM stays clean even when a script hook elsewhere in
// the module graph would happily hand back a body with `function` in it.

const mocks = vi.hoisted(() => ({
  flowErrors: vi.fn(),
  script: vi.fn(),
}));

vi.mock("@/hooks/use-celigo-flows", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/use-celigo-flows")>("@/hooks/use-celigo-flows");
  return {
    ...actual,
    useCeligoFlowErrors: () => mocks.flowErrors(),
    useCeligoScript: () => mocks.script(),
  };
});

import { CeligoStepInspector, type InspectorTab } from "../celigo-step-inspector";

function makeAttachment(overrides: Partial<CeligoAttachment> = {}): CeligoAttachment {
  return {
    id: "att-1",
    flow_id: "flow-1",
    flow_step_id: "step-1",
    script_id: "script-1",
    script_celigo_id: "script-celigo-1",
    function_name: "preSavePage",
    json_path: "66738c3d….hooks.preSavePage",
    site_type: "hook",
    script_name: "sales_order_script_v2",
    script_size_chars: 34099,
    script_copies_count: 1,
    script_versions_count: 1,
    script_version_letter: null,
    script_content_diverged: false,
    ...overrides,
  };
}

function makeStep(overrides: Partial<CeligoFlowStep> & Pick<CeligoFlowStep, "id">): CeligoFlowStep {
  return {
    celigo_id: `cel-${overrides.id}`,
    role: "processor",
    router_id: null,
    branch_id: null,
    branch_key: "",
    sequence: 0,
    adaptor_type: "HTTPExport",
    connection_celigo_id: "648bd44c1234567890abcdef",
    reference_name: null,
    filter_json: null,
    mapping_json: null,
    proceed_on_failure: null,
    skip_retries: null,
    kind: "lookup",
    record_type: null,
    operation: null,
    search_id: null,
    attachments: [],
    error_count: 0,
    ...overrides,
  };
}

function makeRouter(overrides: Partial<CeligoRouter> = {}): CeligoRouter {
  return {
    id: "r1",
    name: null,
    route_records_to: null,
    route_records_using: null,
    has_script_slot: false,
    branches: [],
    ...overrides,
  };
}

function makeDetail(overrides: Partial<CeligoFlowDetail> & { steps: CeligoFlowStep[] }): CeligoFlowDetail {
  return {
    id: "flow-1",
    integration_id: "int-1",
    celigo_id: "cel-flow-1",
    name: "Test flow",
    disabled: false,
    schedule: null,
    timezone: null,
    last_executed_at: null,
    source_id: null,
    ai_description_summary: null,
    ai_description_detailed: null,
    celigo_last_modified: null,
    unassigned_attachments: [],
    routers: [],
    celigo_open_error_count: null,
    last_error_at: null,
    error_count: 0,
    signature_count: 0,
    ...overrides,
  };
}

function makeErrorGroup(overrides: Partial<CeligoFlowErrorGroup> = {}): CeligoFlowErrorGroup {
  return {
    signature: {
      id: "sig-1",
      fingerprint: "fp-1",
      source: "pre_save_page_hook",
      code: "script_error",
      sample_message: "TypeError: Cannot read properties of null (reading 'name')",
      occurrence_count: 10,
      first_seen: "2026-08-17T06:20:00.000Z",
      last_seen: "2026-08-17T11:05:00.000Z",
    },
    count: 10,
    step_ids: ["step-1"],
    first_seen_at: "2026-08-17T06:20:00.000Z",
    last_seen_at: "2026-08-17T11:05:00.000Z",
    retriable: false,
    purge_at: "2026-09-16T00:00:00.000Z",
    trace_keys: ["15822111", "15241110"],
    errors: [],
    ...overrides,
  };
}

const noop = () => {};

function renderInspector(props: {
  detail: CeligoFlowDetail;
  step: CeligoFlowStep | null;
  tab?: InspectorTab;
  onTabChange?: (tab: InspectorTab) => void;
  lastSyncedAt?: string | null;
  onOpenScript?: (scriptId: string, opener: HTMLElement | null, jsonPath: string | null) => void;
}) {
  return render(
    <CeligoStepInspector
      detail={props.detail}
      step={props.step}
      tab={props.tab ?? "facts"}
      onTabChange={props.onTabChange ?? noop}
      // `??` would swallow an EXPLICIT null — the state the fix-wave tests
      // below exercise — so the default only applies when the key is absent.
      lastSyncedAt={"lastSyncedAt" in props ? props.lastSyncedAt! : "2026-09-02T18:12:00.000Z"}
      onOpenScript={props.onOpenScript ?? noop}
    />,
  );
}

beforeEach(() => {
  mocks.flowErrors.mockReturnValue(resolved({ flow_id: "flow-1", status: "open", total: 0, groups: [] }));
  mocks.script.mockReturnValue(
    resolved({
      id: "script-1",
      dedup_key: "dk-1",
      name: "sales_order_script_v2",
      content: "function preSavePage() { return true; }",
      content_hash: "hash-1",
      copies_count: 1,
      attachment_count: 1,
      integration_count: 1,
      content_diverged: false,
      used_by: [],
    }),
  );
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-02T18:16:00.000Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Resting state (no step) — preserved from Task 14's own contract test in
// celigo-flow-page.test.tsx, which this component must keep satisfying even
// though this task never touches that file.
// ---------------------------------------------------------------------------

describe("no step selected", () => {
  it("renders the Task 14 resting-state contract", () => {
    renderInspector({ detail: makeDetail({ steps: [] }), step: null });
    const inspector = screen.getByTestId("celigo-step-inspector-stub");
    expect(inspector).toHaveTextContent("no step selected");
  });
});

// ---------------------------------------------------------------------------
// Facts tab
// ---------------------------------------------------------------------------

describe("Facts tab", () => {
  it("shows adaptor, connection, role line, celigo id, and 'only in this flow' when the step's celigo_id is unique to this flow", () => {
    const step = makeStep({
      id: "step-1",
      celigo_id: "66738c3dabcdef1234567890",
      kind: "lookup",
      router_id: "r1",
      branch_id: "b1",
      sequence: 0,
    });
    const detail = makeDetail({
      steps: [step],
      routers: [makeRouter({ id: "r1", branches: [{ id: "b1", name: "Branch 1", rule_count: 0, next_router_id: null, order: 0, declared_step_count: 1 }] })],
    });
    renderInspector({ detail, step, tab: "facts" });

    expect(screen.getByText("HTTPExport")).toBeInTheDocument();
    expect(screen.getByText("648bd44c… · name not synced")).toBeInTheDocument();
    expect(screen.getByText("lookup · router 1 · branch 1 · step 1 of 1")).toBeInTheDocument();
    expect(screen.getByText("66738c3d…")).toBeInTheDocument();
    expect(screen.getByText("only in this flow")).toBeInTheDocument();
  });

  it("says 'also in Branch 2' when the same celigo_id is wired into a later branch", () => {
    const stepA = makeStep({
      id: "step-a",
      celigo_id: "cel-shared",
      kind: "lookup",
      router_id: "r1",
      branch_id: "b1",
      sequence: 0,
    });
    const stepB = makeStep({
      id: "step-b",
      celigo_id: "cel-shared",
      kind: "lookup",
      router_id: "r1",
      branch_id: "b2",
      sequence: 0,
    });
    const detail = makeDetail({
      steps: [stepA, stepB],
      routers: [
        makeRouter({
          id: "r1",
          branches: [
            { id: "b1", name: "Branch 1", rule_count: 0, next_router_id: null, order: 0, declared_step_count: 1 },
            { id: "b2", name: "Branch 2", rule_count: 0, next_router_id: null, order: 1, declared_step_count: 1 },
          ],
        }),
      ],
    });
    renderInspector({ detail, step: stepA, tab: "facts" });

    expect(screen.getByText("also in Branch 2")).toBeInTheDocument();
  });

  it("numbers a step's position within its own branch by sequence, not array order", () => {
    const first = makeStep({ id: "s-first", router_id: "r1", branch_id: "b1", sequence: 1, kind: "destination" });
    const second = makeStep({ id: "s-second", router_id: "r1", branch_id: "b1", sequence: 2, kind: "destination" });
    const detail = makeDetail({
      // Declared out of sequence order -- the position must follow
      // `sequence`, never the array's own order.
      steps: [second, first],
      routers: [makeRouter({ id: "r1", branches: [{ id: "b1", name: null, rule_count: 0, next_router_id: null, order: 0, declared_step_count: 2 }] })],
    });
    renderInspector({ detail, step: second, tab: "facts" });

    expect(screen.getByText("destination · router 1 · branch 1 · step 2 of 2")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Filter tab
// ---------------------------------------------------------------------------

describe("Filter tab", () => {
  it("renders 'No filter on this step' when filter_json is null", () => {
    const step = makeStep({ id: "step-1", filter_json: null });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "filter" });
    expect(screen.getByText("No filter on this step")).toBeInTheDocument();
  });

  it("renders FilterPanel when filter_json is set", () => {
    const step = makeStep({ id: "step-1", filter_json: { status: "eq" } });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "filter" });
    expect(
      screen.getByText("Determines which records this step processes — the reason a record can go through unmatched."),
    ).toBeInTheDocument();
    expect(screen.getByText("status")).toBeInTheDocument();
    expect(screen.getByText("eq")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Mapping tab
// ---------------------------------------------------------------------------

describe("Mapping tab", () => {
  it("renders the response mapping heading (singular) and the not-synced line", () => {
    const step = makeStep({
      id: "step-1",
      mapping_json: { fields: [{ extract: "customer.name", generate: "entity" }] },
    });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "mapping" });
    expect(screen.getByText("Response mapping · 1 field")).toBeInTheDocument();
    expect(screen.getByText("NetSuite field mapping · not synced")).toBeInTheDocument();
    expect(screen.getByText("entity")).toBeInTheDocument();
    expect(screen.getByText("customer.name")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Scripts tab
// ---------------------------------------------------------------------------

describe("Scripts tab", () => {
  it("renders a site card: chip/name, '1 copy · 1 version', size, json_path verbatim, Open source, and the N2 shield", () => {
    const onOpenScript = vi.fn();
    const step = makeStep({
      id: "step-1",
      attachments: [makeAttachment({ id: "att-1", script_id: "script-1" })],
    });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "scripts", onOpenScript });

    expect(screen.getByText("sales_order_script_v2")).toBeInTheDocument();
    expect(screen.getByText("1 copy · 1 version")).toBeInTheDocument();
    expect(screen.getByText("33.3 KB")).toBeInTheDocument();
    expect(screen.getByText("66738c3d….hooks.preSavePage")).toBeInTheDocument();

    // Codex fix wave, items 24 + 25: the handler also carries WHICH element
    // was clicked (so the drawer can hand focus back on close) and WHICH
    // attachment site it was (one script can be attached at several).
    const opener = screen.getByText("Open source →");
    fireEvent.click(opener);
    expect(onOpenScript).toHaveBeenCalledWith("script-1", opener, "66738c3d….hooks.preSavePage");

    expect(
      screen.getByText(
        "Customer-authored JavaScript, shown to you only. Never run here, never sent to the assistant.",
      ),
    ).toBeInTheDocument();
  });

  it("renders the family form for a diverged, multi-copy script", () => {
    const step = makeStep({
      id: "step-1",
      attachments: [
        makeAttachment({
          id: "att-1",
          function_name: "preMap",
          script_copies_count: 7,
          script_versions_count: 3,
          script_version_letter: "C",
        }),
      ],
    });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "scripts" });
    expect(screen.getByText("copy C of 3 versions · 7 copies")).toBeInTheDocument();
  });

  it("shows 'script body not synced' instead of Open source when script_id is null", () => {
    const step = makeStep({
      id: "step-1",
      attachments: [makeAttachment({ id: "att-1", script_id: null })],
    });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "scripts" });
    expect(screen.getByText("script body not synced")).toBeInTheDocument();
    expect(screen.queryByText("Open source →")).not.toBeInTheDocument();
  });

  it("never renders script content, even though the mocked useCeligoScript would return some", () => {
    const step = makeStep({
      id: "step-1",
      attachments: [makeAttachment({ id: "att-1" })],
    });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "scripts" });
    expect(screen.queryByText(/function /)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Errors tab
// ---------------------------------------------------------------------------

describe("empty strings are absences, not values", () => {
  it("renders an empty adaptor_type as '—' and omits it from the header, exactly like null", () => {
    // Gate fix wave, minor. The header already treated "" as absent
    // (`step.adaptor_type ? …`) while the Facts row used `?? "—"`, which
    // keeps "" and prints a blank value where the dash belongs.
    const step = makeStep({ id: "step-1", adaptor_type: "", kind: "destination" });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "facts" });
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText("Destination")).toBeInTheDocument();
  });

  it("falls back to 'hook' for an empty function_name on a script site", () => {
    const step = makeStep({
      id: "step-1",
      attachments: [makeAttachment({ function_name: "" })],
    });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "scripts" });
    expect(screen.getByText("HK hook")).toBeInTheDocument();
  });
});

describe("Errors tab", () => {
  it("renders the quiet sentence with the sync time when the whole flow is clean", () => {
    mocks.flowErrors.mockReturnValue(resolved({ flow_id: "flow-1", status: "open", total: 0, groups: [] }));
    const step = makeStep({ id: "step-1" });
    renderInspector({
      detail: makeDetail({ steps: [step], error_count: 0 }),
      step,
      tab: "errors",
      lastSyncedAt: "2026-09-02T18:12:00.000Z",
    });
    expect(
      screen.getByText("No open errors. Celigo reported 0 on the last sync, 4 min ago."),
    ).toBeInTheDocument();
  });

  it("says where the flow's OTHER open errors are instead of claiming the flow reported zero", () => {
    // Gate fix wave, item 4. This pane used to say "Celigo reported 0 for the
    // whole flow" whenever no signature touched THIS step -- a flow-wide
    // claim, printed verbatim on a flow with open errors in other steps.
    mocks.flowErrors.mockReturnValue(
      resolved({
        flow_id: "flow-1",
        status: "open",
        total: 3,
        groups: [makeErrorGroup({ step_ids: ["some-other-step"] })],
      }),
    );
    const step = makeStep({ id: "step-1" });
    renderInspector({
      detail: makeDetail({ steps: [step], error_count: 3 }),
      step,
      tab: "errors",
      lastSyncedAt: "2026-09-02T18:12:00.000Z",
    });
    expect(screen.queryByText("script_error")).not.toBeInTheDocument();
    expect(
      screen.getByText("No open errors on this step. 3 open elsewhere in this flow as of the last sync, 4 min ago."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/reported 0/)).not.toBeInTheDocument();
  });

  it("renders a loading state, never an empty one, while the errors query is pending", () => {
    mocks.flowErrors.mockReturnValue(pending());
    const step = makeStep({ id: "step-1" });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "errors" });
    expect(screen.getByText("Loading errors…")).toBeInTheDocument();
    expect(screen.queryByText(/No open errors/)).not.toBeInTheDocument();
  });

  it("renders a visibly distinct error notice, never an empty one, when the errors query fails", () => {
    const refetch = vi.fn();
    mocks.flowErrors.mockReturnValue(errored(refetch));
    const step = makeStep({ id: "step-1" });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "errors" });
    expect(screen.getByText("Couldn't load errors for this step.")).toBeInTheDocument();
    expect(screen.queryByText(/No open errors/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Retry"));
    expect(refetch).toHaveBeenCalled();
  });

  it("renders a signature card for a group whose step_ids include this step", () => {
    mocks.flowErrors.mockReturnValue(
      resolved({
        flow_id: "flow-1",
        status: "open",
        total: 10,
        groups: [makeErrorGroup({ step_ids: ["step-1"] })],
      }),
    );
    const step = makeStep({ id: "step-1" });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "errors" });

    expect(screen.getByText("script_error")).toBeInTheDocument();
    expect(screen.getByText("pre_save_page_hook")).toBeInTheDocument();
    expect(screen.getByText("×10")).toBeInTheDocument();
    expect(screen.getByText("not retriable")).toBeInTheDocument();
    expect(screen.getByText("purges 16 Sep")).toBeInTheDocument();
    expect(screen.getByText("15822111")).toBeInTheDocument();
    expect(screen.getByText("15241110")).toBeInTheDocument();
    // Final-review finding I7: the card used to end in "Open the 10 in recon →"
    // pointing at /reconciliation. These are Celigo integration errors; the
    // reconciliation surface knows nothing about them and shows no such list,
    // so the link led nowhere useful. The trace-key chips stay -- they ARE the
    // handle an operator carries into Celigo.
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("the Errors tab badge counts THIS step's errors, not a shared signature's flow-wide total", () => {
    // Final-review finding I3. The badge summed `group.count` for every group
    // whose `step_ids` include the step, so one 10-error signature spanning two
    // steps read "Errors 10" on BOTH of them. `step.error_count` is the count
    // the backend attributed to this step; the signature card keeps the
    // flow-wide ×10 as the detail.
    mocks.flowErrors.mockReturnValue(
      resolved({
        flow_id: "flow-1",
        status: "open",
        total: 10,
        groups: [makeErrorGroup({ count: 10, step_ids: ["step-1", "step-2"] })],
      }),
    );
    const step = makeStep({ id: "step-1", error_count: 4 });
    const other = makeStep({ id: "step-2", error_count: 6 });
    renderInspector({ detail: makeDetail({ steps: [step, other] }), step, tab: "errors" });

    expect(screen.getByRole("tab", { name: /Errors/ })).toHaveTextContent("Errors 4");
    expect(screen.getByText("×10")).toBeInTheDocument();
  });

  it("the Errors badge reads the step's own count even while the errors query is pending", () => {
    mocks.flowErrors.mockReturnValue(pending());
    const step = makeStep({ id: "step-1", error_count: 4 });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "errors" });
    expect(screen.getByRole("tab", { name: /Errors/ })).toHaveTextContent("Errors 4");
  });

  it("a tab prop of 'errors' selects the Errors tab directly", () => {
    mocks.flowErrors.mockReturnValue(
      resolved({ flow_id: "flow-1", status: "open", total: 10, groups: [makeErrorGroup({ step_ids: ["step-1"] })] }),
    );
    const step = makeStep({ id: "step-1" });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "errors" });
    // No click needed -- the errors-tab-only content is already visible.
    expect(screen.getByText("script_error")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------

describe("tab switching", () => {
  it("fires onTabChange when a different tab is clicked", () => {
    const onTabChange = vi.fn();
    const step = makeStep({ id: "step-1" });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "facts", onTabChange });
    // Radix's Tabs.Trigger activates on `mousedown`, not `click` --
    // matches the established pattern in celigo-integration-page.test.tsx.
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Errors/ }));
    expect(onTabChange).toHaveBeenCalledWith("errors");
  });
});

// ---------------------------------------------------------------------------
// Codex fix wave
// ---------------------------------------------------------------------------

describe("the quiet errors sentence never prints a dash for the sync time (item 2)", () => {
  it("says the sync time is unavailable instead of 'on the last sync, —' on a clean flow", () => {
    mocks.flowErrors.mockReturnValue(resolved({ flow_id: "flow-1", status: "open", total: 0, groups: [] }));
    const step = makeStep({ id: "step-1" });
    const { container } = renderInspector({
      detail: makeDetail({ steps: [step], error_count: 0 }),
      step,
      tab: "errors",
      lastSyncedAt: null,
    });

    expect(screen.getByText("No open errors on this step (sync time unavailable).")).toBeInTheDocument();
    expect(container.textContent).not.toContain("last sync, —");
  });

  it("still says where the flow's other errors are, without the dash", () => {
    mocks.flowErrors.mockReturnValue(
      resolved({ flow_id: "flow-1", status: "open", total: 3, groups: [makeErrorGroup({ step_ids: ["other"] })] }),
    );
    const step = makeStep({ id: "step-1" });
    const { container } = renderInspector({
      detail: makeDetail({ steps: [step], error_count: 3 }),
      step,
      tab: "errors",
      lastSyncedAt: null,
    });

    expect(container.textContent).toContain("No open errors on this step (sync time unavailable).");
    expect(container.textContent).toContain("3 open elsewhere in this flow");
    expect(container.textContent).not.toContain("last sync, —");
  });
});

describe("an unknown adaptor is never called HTTP (item 6)", () => {
  it("titles the step by its kind and says the adaptor is not synced", () => {
    const step = makeStep({ id: "step-1", kind: "destination", adaptor_type: null, reference_name: null });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "facts" });

    expect(screen.getByText("Destination · adaptor not synced")).toBeInTheDocument();
    expect(screen.queryByText(/HTTP/)).not.toBeInTheDocument();
  });
});

describe("the position line does not conflate a null router or branch id (item 16)", () => {
  it("says 'top level' for a step with no router at all", () => {
    const step = makeStep({ id: "step-1", kind: "lookup", router_id: null, branch_id: null });
    const other = makeStep({ id: "step-2", kind: "lookup", router_id: null, branch_id: null, sequence: 1 });
    renderInspector({ detail: makeDetail({ steps: [step, other] }), step, tab: "facts" });

    expect(screen.getByText("lookup · top level · step 1 of 2")).toBeInTheDocument();
  });

  it("says 'branch id not synced' rather than naming a branch it cannot identify", () => {
    const step = makeStep({ id: "step-1", kind: "lookup", router_id: "r1", branch_id: null });
    const detail = makeDetail({
      steps: [step],
      routers: [
        makeRouter({
          id: "r1",
          branches: [{ id: null, name: null, rule_count: 0, next_router_id: null, order: 0, declared_step_count: 1 }],
        }),
      ],
    });
    renderInspector({ detail, step, tab: "facts" });

    expect(screen.getByText("lookup · router 1 · branch id not synced · step 1 of 1")).toBeInTheDocument();
  });
});

describe("the five-tab strip survives the panel's 20% minimum (item 23)", () => {
  it("lets the tab list wrap instead of clipping its last tabs", () => {
    const step = makeStep({ id: "step-1" });
    renderInspector({ detail: makeDetail({ steps: [step] }), step, tab: "facts" });

    expect(screen.getByRole("tablist")).toHaveClass("flex-wrap");
  });
});
