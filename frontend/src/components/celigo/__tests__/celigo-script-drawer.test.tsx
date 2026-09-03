import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { resolved, pending, errored } from "./query-fixtures";

// Task 17 — the script drawer (mockup screen 4): the existing script viewer
// re-homed from a centered Dialog into a right-hand panel over the
// inspector. Mocks the hooks module the same way celigo-script-viewer.test
// (Task 10) does — no MSW, no real apiClient.

const mocks = vi.hoisted(() => ({
  script: vi.fn(),
}));

vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoScript: (scriptId: string | undefined) => mocks.script(scriptId),
}));

import { CeligoScriptDrawer } from "../celigo-script-drawer";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// The real diverged preMap family the approved mockup (screen 4) draws:
// "Add New Sales Order (Framework BV)" — 4 sites of THIS copy across 2
// flows (Branch 1 and Branch 2 of the Multi-Subsidiary flow), plus one
// further clone ("scr-other") collapsed under `copies_count` (7 total).
const siteA = {
  flow_id: "flow-1",
  flow_name: "New Sales Order to NetSuite - Multi-Subsidiary",
  integration_id: "int-1",
  flow_step_id: "step-5",
  flow_step_role: "processor",
  flow_step_adaptor_type: "NetSuiteDistributedImport",
  script_celigo_id: "scr-current",
  json_path: "66738c3d….hooks.preMap",
  function_name: "preMap",
  site_type: "hook",
};
const siteB = { ...siteA, flow_step_id: "step-6", json_path: "6813b3ce….hooks.preMap" };
const siteC = {
  ...siteA,
  flow_id: "flow-2",
  flow_name: "NS > Solidus - Shipping Confirmations v2",
  flow_step_id: "step-7",
  json_path: "a1b2c3d4….hooks.preMap",
};
const siteD = {
  ...siteA,
  flow_id: "flow-2",
  flow_name: "NS > Solidus - Shipping Confirmations v2",
  flow_step_id: "step-8",
  json_path: "e5f6a7b8….hooks.preMap",
};
const otherCopySite = {
  ...siteA,
  script_celigo_id: "scr-other",
  flow_id: "flow-3",
  flow_name: "NS - Create Customer Deposits",
  flow_step_id: "step-9",
  json_path: "9c8d7e6f….hooks.preMap",
};

const SCRIPT_CONTENT = "function preMap(options) {\n  return options;\n}";

const SCRIPT = {
  id: "scr-1",
  dedup_key: "dk-1",
  name: "ns_sales_order_premap",
  content: SCRIPT_CONTENT,
  content_hash: "hash-1",
  copies_count: 7,
  attachment_count: 5,
  integration_count: 2,
  content_diverged: true,
  used_by: [siteA, siteB, siteC, siteD, otherCopySite],
};

beforeEach(() => {
  mocks.script.mockReset();
});

describe("CeligoScriptDrawer — mount/unmount", () => {
  it("renders nothing, and leaks no script content, when scriptId is null", () => {
    mocks.script.mockReturnValue(resolved(SCRIPT));
    wrap(<CeligoScriptDrawer scriptId={null} onClose={vi.fn()} />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SCRIPT_CONTENT);
    expect(document.body.textContent).not.toContain("ns_sales_order_premap");
  });
});

describe("CeligoScriptDrawer — pending / error", () => {
  it("shows a skeleton while the query is pending — not the empty or loaded body", () => {
    mocks.script.mockReturnValue(pending());
    wrap(<CeligoScriptDrawer scriptId="scr-1" onClose={vi.fn()} />);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SCRIPT_CONTENT);
    expect(screen.queryByText(/couldn.?t load/i)).not.toBeInTheDocument();
  });

  it("shows an error notice, not a skeleton, when the query fails — and Retry calls refetch", () => {
    const refetch = vi.fn();
    mocks.script.mockReturnValue(errored(refetch));
    wrap(<CeligoScriptDrawer scriptId="scr-1" onClose={vi.fn()} />);

    expect(screen.getByText(/couldn.?t load this script/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });
});

describe("CeligoScriptDrawer — loaded, as a right-panel drawer over the inspector", () => {
  it("renders role=dialog aria-label='Script source', with the drawer's fixed right-panel classes", () => {
    mocks.script.mockReturnValue(resolved(SCRIPT));
    wrap(<CeligoScriptDrawer scriptId="scr-1" onClose={vi.fn()} />);

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-label", "Script source");
    for (const cls of [
      "fixed",
      "inset-y-0",
      "right-0",
      "h-full",
      "w-[560px]",
      "max-w-[95vw]",
      "translate-x-0",
      "translate-y-0",
      "rounded-none",
      "border-l",
    ]) {
      expect(dialog.className).toContain(cls);
    }
  });

  it("renders the header hook chip + name, the copies/diverged and sites/flows pills, the code, the N2 banner, the Scripts-view text, and the used-by rows", () => {
    mocks.script.mockReturnValue(resolved(SCRIPT));
    wrap(<CeligoScriptDrawer scriptId="scr-1" onClose={vi.fn()} />);

    // Header: "HK preMap · ns_sales_order_premap" (task brief, verbatim).
    expect(screen.getByText("HK preMap")).toBeInTheDocument();
    expect(screen.getByText("ns_sales_order_premap")).toBeInTheDocument();

    // Pills: "7 copies · diverged" (family), "4 sites · 2 flows" (this
    // copy's own sites, from used_by).
    expect(screen.getByText("7 copies · diverged")).toBeInTheDocument();
    expect(screen.getByText("4 sites · 2 flows")).toBeInTheDocument();

    // Body: the syntax highlighter renders `content` as inert text. Checked
    // per-line, not as one contiguous block — `showLineNumbers` interleaves
    // a line-number span before each wrapped line, so the raw multi-line
    // string is never a single contiguous substring of `textContent` (the
    // single-line fixture in celigo-script-viewer.test.tsx never hits this,
    // since it has only one line to interleave a number into).
    expect(document.body.textContent).toContain("function preMap(options) {");
    expect(document.body.textContent).toContain("return options;");

    // Banner: N2's exact copy, verbatim.
    expect(
      screen.getByText(
        "Customer-authored JavaScript, shown to you only. Never run here, never sent to the assistant.",
      ),
    ).toBeInTheDocument();

    // "Scripts view ↗" — present, per the brief; not wired to a real
    // destination (see celigo-script-viewer.tsx's own comment on why).
    expect(screen.getByText("Scripts view ↗")).toBeInTheDocument();

    // Used-by rows: the 4 explicit sites belonging to THIS copy
    // (script_celigo_id "scr-current"), across the 2 flows. The OTHER
    // clone's own site ("scr-other") never renders explicitly — only
    // `copies_count - 1` (7 - 1 = 6) collapses into the summary row;
    // `used_by` need not enumerate every one of the 7 clones' sites for
    // that count to be correct (same as the pre-Task-17 dialog's own
    // 20-clone fixture).
    expect(screen.getByText("66738c3d….hooks.preMap")).toBeInTheDocument();
    expect(screen.getByText("6813b3ce….hooks.preMap")).toBeInTheDocument();
    expect(screen.getByText("a1b2c3d4….hooks.preMap")).toBeInTheDocument();
    expect(screen.getByText("e5f6a7b8….hooks.preMap")).toBeInTheDocument();
    expect(screen.queryByText("9c8d7e6f….hooks.preMap")).not.toBeInTheDocument();
    expect(screen.getByText(/6 further cop/i)).toBeInTheDocument();
  });
});

describe("CeligoScriptDrawer — currentStepId", () => {
  it("names the site of the step the drawer was opened FROM, not used_by[0]", () => {
    // Gate fix wave, item 10. `CeligoScriptViewerBody` has always accepted
    // `currentStepId` and falls back to `used_by[0]` without it -- but no
    // caller ever passed one, so a script attached at several sites always
    // announced the FIRST site's hook, whichever step you actually came from.
    const first = { ...siteA, flow_step_id: "step-1", function_name: "preSavePage" };
    const current = { ...siteA, flow_step_id: "step-42", function_name: "preMap" };
    mocks.script.mockReturnValue(resolved({ ...SCRIPT, used_by: [first, current] }));

    wrap(<CeligoScriptDrawer scriptId="scr-1" currentStepId="step-42" onClose={vi.fn()} />);

    expect(screen.getByText("HK preMap")).toBeInTheDocument();
    expect(screen.queryByText("HK preSavePage")).not.toBeInTheDocument();
  });

  it("still falls back to the first site when no step is selected", () => {
    const first = { ...siteA, flow_step_id: "step-1", function_name: "preSavePage" };
    const other = { ...siteA, flow_step_id: "step-42", function_name: "preMap" };
    mocks.script.mockReturnValue(resolved({ ...SCRIPT, used_by: [first, other] }));

    wrap(<CeligoScriptDrawer scriptId="scr-1" currentStepId={null} onClose={vi.fn()} />);

    expect(screen.getByText("HK preSavePage")).toBeInTheDocument();
  });
});

describe("CeligoScriptDrawer — Escape", () => {
  it("calls onClose on Escape (Radix Dialog's own default dismissal — no drawer-local keydown listener)", () => {
    mocks.script.mockReturnValue(resolved(SCRIPT));
    const onClose = vi.fn();
    wrap(<CeligoScriptDrawer scriptId="scr-1" onClose={onClose} />);

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});

describe("CeligoScriptDrawer — focus management", () => {
  it("focuses the close button on open, and returns focus to returnFocusTo when it closes", async () => {
    mocks.script.mockReturnValue(resolved(SCRIPT));
    const anchor = document.createElement("button");
    anchor.textContent = "Open source";
    document.body.appendChild(anchor);
    const returnFocusTo = { current: anchor as HTMLElement | null };

    const { rerender } = wrap(
      <CeligoScriptDrawer scriptId="scr-1" onClose={vi.fn()} returnFocusTo={returnFocusTo} />,
    );

    await waitFor(() => expect(screen.getByRole("button", { name: /close/i })).toHaveFocus());

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    rerender(
      <QueryClientProvider client={qc}>
        <CeligoScriptDrawer scriptId={null} onClose={vi.fn()} returnFocusTo={returnFocusTo} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(anchor).toHaveFocus());
    document.body.removeChild(anchor);
  });
});
