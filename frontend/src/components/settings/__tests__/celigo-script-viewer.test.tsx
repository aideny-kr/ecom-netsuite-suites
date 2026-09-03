import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

// Task 10 — script viewer (mockup screen 04). Mocks use-celigo-flows
// entirely, matching celigo-flow-map.test.tsx's established pattern of
// mocking the hooks module rather than apiClient.

const mocks = vi.hoisted(() => ({
  script: vi.fn(),
}));

vi.mock("@/hooks/use-celigo-flows", () => ({
  useCeligoScript: (scriptId: string | undefined) => mocks.script(scriptId),
}));

import { CeligoScriptViewerDialog } from "../celigo-script-viewer";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const siteA = {
  flow_id: "flow-1",
  flow_name: "Inventory Sync",
  integration_id: "int-1",
  flow_step_id: "step-1",
  flow_step_role: "processor",
  flow_step_adaptor_type: "NetSuiteDistributedImport",
  script_celigo_id: "scr-1",
  json_path: "pageProcessors[0].transform.script",
  function_name: "transform",
  site_type: "transform",
};

const siteB = {
  ...siteA,
  json_path: "hooks.preSavePage",
  function_name: "preSave",
  site_type: "hook",
};

const baseScript = {
  id: "scr-local-1",
  dedup_key: "dk-1",
  name: "BigQuery Data Warehouse Script[v1.1.0]",
  content: "function transform(record) { return record; }",
  content_hash: "hash-1",
  copies_count: 1,
  attachment_count: 2,
  integration_count: 1,
  content_diverged: false,
  used_by: [siteA, siteB],
};

function cloneFamily(overrides: Partial<typeof baseScript> = {}) {
  const sites = Array.from({ length: 20 }, (_, i) => ({
    ...siteA,
    flow_id: `flow-${i}`,
    flow_name: `Flow ${i}`,
    flow_step_id: `step-${i}`,
    script_celigo_id: `scr-${i}`,
  }));
  return {
    ...baseScript,
    copies_count: 20,
    attachment_count: 20,
    used_by: sites,
    ...overrides,
  };
}

beforeEach(() => {
  mocks.script.mockReset();
});

describe("CeligoScriptViewerDialog — loading and error states", () => {
  it("shows a loading state while the query is in flight", () => {
    mocks.script.mockReturnValue({ data: undefined, isPending: true, isLoading: true, isError: false, refetch: vi.fn() });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(screen.getByText(/loading script/i)).toBeInTheDocument();
  });

  it("renders an error state, not an empty state, when the query fails", () => {
    mocks.script.mockReturnValue({ data: undefined, isLoading: false, isError: true, refetch: vi.fn() });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(screen.getByText(/couldn.?t load/i)).toBeInTheDocument();
    expect(screen.queryByText(/no attachment sites recorded/i)).not.toBeInTheDocument();
  });

  it("a retry button on the error state calls refetch", () => {
    const refetch = vi.fn();
    mocks.script.mockReturnValue({ data: undefined, isLoading: false, isError: true, refetch });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });

  it("shows a genuinely-empty state (distinct from error) when a script has no recorded attachment sites", () => {
    mocks.script.mockReturnValue({
      data: { ...baseScript, used_by: [] },
      isPending: false,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(screen.getByText(/no attachment sites recorded/i)).toBeInTheDocument();
    expect(screen.queryByText(/couldn.?t load/i)).not.toBeInTheDocument();
  });
});

describe("CeligoScriptViewerDialog — card head", () => {
  // Task 17 -- the pill format changed from "N copies · M integrations" to
  // "N copies[ · diverged]" + a separate "N sites · M flows" pill (used_by-
  // derived), matching the extracted `CeligoScriptViewerBody` the script
  // drawer now shares. `baseScript` has both sites on the SAME flow_id
  // ("flow-1"), so the sites/flows pill reads "2 sites · 1 flows".
  it("shows the script name, the hook chip, and the copies/sites pills", () => {
    mocks.script.mockReturnValue({ data: baseScript, isPending: false, isLoading: false, isError: false, refetch: vi.fn() });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(screen.getByText("BigQuery Data Warehouse Script[v1.1.0]")).toBeInTheDocument();
    expect(screen.getByText("HK transform")).toBeInTheDocument();
    expect(screen.getByText("1 copies")).toBeInTheDocument();
    expect(screen.getByText("2 sites · 1 flows")).toBeInTheDocument();
  });
});

describe("CeligoScriptViewerDialog — attachment table", () => {
  it("renders BOTH sites for a script attached at both transform.script and hooks.preSavePage", () => {
    mocks.script.mockReturnValue({ data: baseScript, isPending: false, isLoading: false, isError: false, refetch: vi.fn() });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(screen.getByText("pageProcessors[0].transform.script")).toBeInTheDocument();
    expect(screen.getByText("hooks.preSavePage")).toBeInTheDocument();
    expect(screen.getAllByText("Inventory Sync")).toHaveLength(2);
  });

  it("collapses ~20 clone copies into a single summary row instead of 20 explicit rows", () => {
    mocks.script.mockReturnValue({ data: cloneFamily(), isPending: false, isLoading: false, isError: false, refetch: vi.fn() });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    const rows = screen.getAllByRole("row");
    // header row + 1 explicit attachment row + 1 collapse row
    expect(rows).toHaveLength(3);
    expect(screen.getByText(/19 further copies/i)).toBeInTheDocument();
  });

  it("guards an empty-string function_name the same as null (not just missing)", () => {
    mocks.script.mockReturnValue({
      data: { ...baseScript, used_by: [{ ...siteA, function_name: "" }] },
      isPending: false,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});

describe("CeligoScriptViewerDialog — content_diverged correction", () => {
  it("says 'identical source' when content_diverged is false", () => {
    mocks.script.mockReturnValue({
      data: cloneFamily({ content_diverged: false }),
      isPending: false,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(screen.getByText(/identical source/i)).toBeInTheDocument();
  });

  it("says the copies differ, and that the shown source is only this copy's own version, when content_diverged is true", () => {
    mocks.script.mockReturnValue({
      data: cloneFamily({ content_diverged: true }),
      isPending: false,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(screen.queryByText(/identical source/i)).not.toBeInTheDocument();
    expect(screen.getByText(/differ/i)).toBeInTheDocument();
    expect(screen.getByText(/this copy's own version/i)).toBeInTheDocument();
  });
});

describe("CeligoScriptViewerDialog — untrusted content", () => {
  it("renders the script source and the untrusted-content banner", () => {
    mocks.script.mockReturnValue({ data: baseScript, isPending: false, isLoading: false, isError: false, refetch: vi.fn() });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    // The dialog renders into a portal on document.body, and the syntax
    // highlighter splits source into multiple token spans, so a
    // single-node text match won't find it -- assert against the full
    // rendered body text instead.
    expect(document.body.textContent).toContain("function transform(record) { return record; }");
    // Task 17 -- N2's exact, project-wide banner copy replaces the old
    // "quoted to the assistant inside a sealed block" line (which wrongly
    // implied script content ever reaches a chat/tool path).
    expect(
      screen.getByText("Customer-authored JavaScript, shown to you only. Never run here, never sent to the assistant."),
    ).toBeInTheDocument();
  });

  // Fix round 1 -- `??` does not catch `""`. This file defines `displayOr`
  // specifically to guard that (see its docstring), and routes every OTHER
  // optional field through it -- `content` was the one field left on `??`.
  it("shows the 'no source recorded' placeholder when content is an empty string, not a blank code block", () => {
    mocks.script.mockReturnValue({
      data: { ...baseScript, content: "" },
      isPending: false,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(document.body.textContent).toContain("// No source recorded for this script.");
  });
});
