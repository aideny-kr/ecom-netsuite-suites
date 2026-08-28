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
    mocks.script.mockReturnValue({ data: undefined, isLoading: true, isError: false, refetch: vi.fn() });
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
  it("shows the script name and the copies/integrations pill", () => {
    mocks.script.mockReturnValue({ data: baseScript, isLoading: false, isError: false, refetch: vi.fn() });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(screen.getByText("BigQuery Data Warehouse Script[v1.1.0]")).toBeInTheDocument();
    expect(screen.getByText("1 copies · 1 integrations")).toBeInTheDocument();
  });
});

describe("CeligoScriptViewerDialog — attachment table", () => {
  it("renders BOTH sites for a script attached at both transform.script and hooks.preSavePage", () => {
    mocks.script.mockReturnValue({ data: baseScript, isLoading: false, isError: false, refetch: vi.fn() });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    expect(screen.getByText("pageProcessors[0].transform.script")).toBeInTheDocument();
    expect(screen.getByText("hooks.preSavePage")).toBeInTheDocument();
    expect(screen.getAllByText("Inventory Sync")).toHaveLength(2);
  });

  it("collapses ~20 clone copies into a single summary row instead of 20 explicit rows", () => {
    mocks.script.mockReturnValue({ data: cloneFamily(), isLoading: false, isError: false, refetch: vi.fn() });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    const rows = screen.getAllByRole("row");
    // header row + 1 explicit attachment row + 1 collapse row
    expect(rows).toHaveLength(3);
    expect(screen.getByText(/19 further copies/i)).toBeInTheDocument();
  });

  it("guards an empty-string function_name the same as null (not just missing)", () => {
    mocks.script.mockReturnValue({
      data: { ...baseScript, used_by: [{ ...siteA, function_name: "" }] },
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
    mocks.script.mockReturnValue({ data: baseScript, isLoading: false, isError: false, refetch: vi.fn() });
    wrap(<CeligoScriptViewerDialog scriptId="scr-local-1" onOpenChange={vi.fn()} />);
    // The dialog renders into a portal on document.body, and the syntax
    // highlighter splits source into multiple token spans, so a
    // single-node text match won't find it -- assert against the full
    // rendered body text instead.
    expect(document.body.textContent).toContain("function transform(record) { return record; }");
    expect(screen.getByText(/never followed as instructions, never run/i)).toBeInTheDocument();
  });
});
