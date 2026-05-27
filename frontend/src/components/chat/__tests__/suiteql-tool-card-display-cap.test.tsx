import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import type { ToolCallStep } from "@/lib/types";

vi.mock("lucide-react", () => {
  const MockIcon = ({ className }: { className?: string }) => <span data-testid="icon" className={className} />;
  return {
    ChevronDown: MockIcon,
    Database: MockIcon,
    Bookmark: MockIcon,
    Check: MockIcon,
    Download: MockIcon,
    FileSpreadsheet: MockIcon,
    Loader2: MockIcon,
    X: MockIcon,
    Pencil: MockIcon,
  };
});

const exportToExcelMock = vi.fn();
const exportFromQueryMock = vi.fn();
vi.mock("@/hooks/use-excel-export", () => ({
  useExcelExport: () => ({
    exportToExcel: exportToExcelMock,
    exportFromQuery: exportFromQueryMock,
    isExporting: false,
  }),
}));

vi.mock("@/hooks/use-saved-queries", () => ({
  useCreateSavedQuery: () => ({ mutate: vi.fn() }),
}));

// Import AFTER mocks
import { SuiteQLToolCard } from "../suiteql-tool-card";

function buildStep(rowCount: number, opts: { truncated?: boolean } = {}): ToolCallStep {
  const rows: unknown[][] = Array.from({ length: rowCount }, (_, i) => [i, `row-${i}`, `val-${i}`]);
  return {
    tool: "netsuite_suiteql",
    step: 1,
    status: "complete",
    duration_ms: 100,
    success: true,
    params: { query: "SELECT id, name, val FROM transaction" },
    result_payload: {
      kind: "table",
      columns: ["id", "name", "val"],
      rows,
      row_count: rowCount,
      truncated: opts.truncated ?? false,
      query: "SELECT id, name, val FROM transaction",
      limit: rowCount,
    },
    result_summary: `${rowCount} rows returned`,
  } as unknown as ToolCallStep;
}

beforeEach(() => {
  exportToExcelMock.mockReset();
  exportFromQueryMock.mockReset();
  // jsdom doesn't implement URL.createObjectURL — stub it so the inline CSV
  // path doesn't blow up when we DO test it.
  (URL as unknown as { createObjectURL: () => string }).createObjectURL = vi.fn(() => "blob:test");
  (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = vi.fn();
});

describe("SuiteQLToolCard display cap", () => {
  it("renders all rows when payload is under the 1000-row display cap", () => {
    render(<SuiteQLToolCard step={buildStep(50)} />);
    // 50 data rows + 1 header row
    const rowEls = screen.getAllByRole("row");
    expect(rowEls.length).toBe(51);
  });

  it("caps rendered rows at 1000 when payload is larger", () => {
    render(<SuiteQLToolCard step={buildStep(2500)} />);
    const rowEls = screen.getAllByRole("row");
    // 1000 data rows + 1 header row
    expect(rowEls.length).toBe(1001);
  });

  it("shows a display-cap banner explaining export when over cap", () => {
    render(<SuiteQLToolCard step={buildStep(2500)} />);
    expect(screen.getByText(/showing 1,?000 of 2,?500 rows/i)).toBeInTheDocument();
    expect(screen.getByText(/export.*full/i)).toBeInTheDocument();
  });
});

describe("SuiteQLToolCard CSV export routing", () => {
  it("uses the inline CSV path for small payloads (no server call)", () => {
    render(<SuiteQLToolCard step={buildStep(50)} />);
    fireEvent.click(screen.getByText(/export csv/i));
    expect(exportFromQueryMock).not.toHaveBeenCalled();
  });

  it("routes CSV through the server when display cap exceeded", () => {
    render(<SuiteQLToolCard step={buildStep(2500)} />);
    fireEvent.click(screen.getByText(/export csv/i));
    expect(exportFromQueryMock).toHaveBeenCalledTimes(1);
    expect(exportFromQueryMock.mock.calls[0][0]).toMatchObject({ format: "csv" });
  });

  it("routes CSV through the server when backend marked the result truncated", () => {
    render(<SuiteQLToolCard step={buildStep(100, { truncated: true })} />);
    fireEvent.click(screen.getByText(/export csv/i));
    expect(exportFromQueryMock).toHaveBeenCalledTimes(1);
    expect(exportFromQueryMock.mock.calls[0][0]).toMatchObject({ format: "csv" });
  });
});
