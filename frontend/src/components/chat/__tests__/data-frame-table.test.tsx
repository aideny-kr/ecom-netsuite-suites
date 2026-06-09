import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

// --- Mock lucide-react (no real SVGs in jsdom) ---
vi.mock("lucide-react", () => {
  const MockIcon = ({ className }: { className?: string }) => (
    <span data-testid="icon" className={className} />
  );
  return {
    ArrowUpDown: MockIcon,
    ArrowUp: MockIcon,
    ArrowDown: MockIcon,
    Copy: MockIcon,
    Check: MockIcon,
    Download: MockIcon,
    FileSpreadsheet: MockIcon,
    Bookmark: MockIcon,
    Loader2: MockIcon,
    Pencil: MockIcon,
    X: MockIcon,
    ChevronDown: MockIcon,
    ChevronUp: MockIcon,
    Code2: MockIcon,
  };
});

// --- Mock table UI components (shadcn wrappers) ---
vi.mock("@/components/ui/table", () => ({
  TableBody: ({ children }: { children: React.ReactNode }) => <tbody>{children}</tbody>,
  TableCell: ({ children, className }: { children?: React.ReactNode; className?: string }) => (
    <td className={className}>{children}</td>
  ),
  TableHead: ({ children, className, onClick }: { children?: React.ReactNode; className?: string; onClick?: () => void }) => (
    <th className={className} onClick={onClick}>{children}</th>
  ),
  TableHeader: ({ children }: { children: React.ReactNode }) => <thead>{children}</thead>,
  TableRow: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <tr className={className}>{children}</tr>
  ),
}));

// --- Mock hooks ---
vi.mock("@/hooks/use-excel-export", () => ({
  useExcelExport: () => ({
    exportToExcel: vi.fn(),
    exportFromQuery: vi.fn(),
    isExporting: false,
  }),
}));

vi.mock("@/hooks/use-saved-queries", () => ({
  useCreateSavedQuery: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
}));

// Import AFTER mocks
import { DataFrameTable } from "../data-frame-table";
import { coerceDataTableData } from "@/lib/chat-stream";
import type { DataTableData } from "@/lib/chat-stream";

function makeMetricData(overrides: Partial<DataTableData> = {}): DataTableData {
  return {
    columns: ["Metric", "Value", "Unit", "Period"],
    rows: [["Net Margin", "—", "percent", "Q1 FY2026"]],
    row_count: 1,
    query: "net_margin",
    truncated: false,
    isMetric: true,
    ...overrides,
  };
}

function makeQueryData(overrides: Partial<DataTableData> = {}): DataTableData {
  return {
    columns: ["tranid", "amount"],
    rows: [["T-001", 500]],
    row_count: 1,
    query: "SELECT tranid, amount FROM transaction FETCH FIRST 1 ROWS ONLY",
    truncated: false,
    isMetric: false,
    ...overrides,
  };
}

beforeEach(() => {
  // jsdom doesn't implement clipboard or URL.createObjectURL
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn() },
  });
  (URL as unknown as { createObjectURL: () => string }).createObjectURL = vi.fn(() => "blob:test");
  (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = vi.fn();
});

describe("DataFrameTable — metric table (isMetric: true)", () => {
  it("hides SuiteQL query affordances for a metric data_table even when queryText is set", () => {
    // In production, message-list passes queryText={block.data.query} for ALL tables,
    // so a metric table gets queryText="net_margin". The component must gate on isMetric.
    render(<DataFrameTable data={makeMetricData()} queryText="net_margin" />);
    // The "SuiteQL Query" expander must NOT appear
    expect(screen.queryByText(/SuiteQL Query/i)).toBeNull();
    // The "Save to Analytics" / "Save Query" button must NOT appear
    expect(screen.queryByRole("button", { name: /save to analytics/i })).toBeNull();
  });

  it("hides the BigQuery SQL expander for a metric data_table", () => {
    render(<DataFrameTable data={makeMetricData()} queryText="net_margin" />);
    expect(screen.queryByText(/BigQuery SQL/i)).toBeNull();
  });

  it("renders the metric data rows (columns and values)", () => {
    render(<DataFrameTable data={makeMetricData()} queryText="net_margin" />);
    // Column headers should be present
    expect(screen.getByText("Metric")).toBeInTheDocument();
    expect(screen.getByText("Value")).toBeInTheDocument();
    // The metric value row should be present
    expect(screen.getByText("Net Margin")).toBeInTheDocument();
    expect(screen.getByText("percent")).toBeInTheDocument();
    expect(screen.getByText("Q1 FY2026")).toBeInTheDocument();
  });
});

describe("DataFrameTable — regular SuiteQL table (isMetric: false / absent)", () => {
  it("shows SuiteQL query affordances for a regular data_table with queryText", () => {
    render(
      <DataFrameTable
        data={makeQueryData()}
        queryText="SELECT tranid, amount FROM transaction FETCH FIRST 1 ROWS ONLY"
      />,
    );
    // The SuiteQL Query expander SHOULD be present for a normal query
    expect(screen.getByText(/SuiteQL Query/i)).toBeInTheDocument();
  });

  it("shows Save to Analytics footer button for a regular data_table with queryText", () => {
    render(
      <DataFrameTable
        data={makeQueryData()}
        queryText="SELECT tranid, amount FROM transaction FETCH FIRST 1 ROWS ONLY"
      />,
    );
    expect(screen.getByRole("button", { name: /save to analytics/i })).toBeInTheDocument();
  });
});

describe("DataFrameTable — REHYDRATION via coerceDataTableData (persisted payload, not hand-set isMetric)", () => {
  // The EXACT persisted metric payload from metric_compute.py (carries snake_case suppress_llm_value).
  const persistedMetricPayload = {
    columns: ["Metric", "Value", "Unit", "Period"],
    rows: [["Net Margin", "12.3", "percent", "Q1 FY2026"]],
    row_count: 1,
    query: "net_margin",
    truncated: false,
    suppress_llm_value: true,
  };

  // A persisted plain-SuiteQL payload — no metric flag.
  const persistedSuiteqlPayload = {
    columns: ["tranid", "amount"],
    rows: [["T-001", 500]],
    row_count: 1,
    query: "SELECT tranid, amount FROM transaction FETCH FIRST 1 ROWS ONLY",
    truncated: false,
  };

  it("hides ALL SuiteQL affordances when the table is built from the persisted metric payload (the real hydration path)", () => {
    // Build the table the way hydration now does — through coerceDataTableData, NOT by hand-setting isMetric.
    const hydrated = coerceDataTableData(persistedMetricPayload);
    render(<DataFrameTable data={hydrated} queryText={hydrated.query} />);
    expect(screen.queryByText(/SuiteQL Query/i)).toBeNull();
    expect(screen.queryByText(/BigQuery SQL/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /save to analytics/i })).toBeNull();
  });

  it("counter-test: SHOWS SuiteQL affordances when built from a persisted plain-SuiteQL payload (no over-suppression)", () => {
    const hydrated = coerceDataTableData(persistedSuiteqlPayload);
    render(<DataFrameTable data={hydrated} queryText={hydrated.query} />);
    expect(screen.getByText(/SuiteQL Query/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save to analytics/i })).toBeInTheDocument();
  });
});

describe("chat-stream: normalizeStreamEvent — data_table with suppress_llm_value", () => {
  it("sets isMetric: true when payload has suppress_llm_value: true", async () => {
    const { normalizeStreamEvent } = await import("@/lib/chat-stream");
    const event = normalizeStreamEvent({
      type: "data_table",
      data: {
        columns: ["Metric", "Value"],
        rows: [["Net Margin", "12.3"]],
        row_count: 1,
        query: "net_margin",
        truncated: false,
        suppress_llm_value: true,
      },
    });
    expect(event).not.toBeNull();
    expect(event?.type).toBe("data_table");
    if (event?.type === "data_table") {
      expect(event.data.isMetric).toBe(true);
    }
  });

  it("sets isMetric: false when suppress_llm_value is absent", async () => {
    const { normalizeStreamEvent } = await import("@/lib/chat-stream");
    const event = normalizeStreamEvent({
      type: "data_table",
      data: {
        columns: ["tranid"],
        rows: [["T-001"]],
        row_count: 1,
        query: "SELECT tranid FROM transaction",
        truncated: false,
      },
    });
    expect(event).not.toBeNull();
    if (event?.type === "data_table") {
      expect(event.data.isMetric).toBe(false);
    }
  });
});
