// Independent packet review round 1 on #370 (gpt-6-astra): one regression per finding.
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeAll, describe, expect, it, vi } from "vitest";
import React from "react";
import { MessageList } from "../message-list";
import { ResultCard } from "../result-card";
import { ToolActivityRow, activityStepsFromCalls, activityStepsFromStream } from "../tool-activity-row";
import { coerceResultCard } from "@/lib/chat-stream";
import type { ResultCardData, StreamBlock } from "@/lib/chat-stream";
import type { ToolCallStep } from "@/lib/types";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn(async () => ({})), post: vi.fn(async () => ({})) },
}));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => ({ user: null }) }));
const exportToExcel = vi.fn();
vi.mock("@/hooks/use-excel-export", () => ({
  useExcelExport: () => ({ exportToExcel, exportFromQuery: vi.fn(), isExporting: false }),
}));

beforeAll(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  (global as { ResizeObserver?: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

const base = {
  card_id: "c1",
  kind: "table",
  result_ids: ["r1"],
  title: "By country",
  source: "NetSuite",
  queries: [],
  columns: [
    { key: "country", label: "Country", format: "text", align: "left" },
    { key: "units", label: "Units", format: "integer", align: "right" },
  ],
  rows: [["US", 5]],
  tiles: [],
  top_n: 1,
  collapsed: false,
};

describe("packet review round 1 on #370", () => {
  it("R1: drops every render field that is not the right type instead of crashing", () => {
    const card = coerceResultCard({
      ...base,
      kind: { evil: true },
      source: 5,
      headline: { message: "x" },
      detail: ["x"],
      check: { status: "warn", text: { message: "Partial" } },
      share: { label: { x: 1 }, of: 1, values: [100] },
      totals_label: { x: 1 },
      more_label: 3,
      less_label: {},
      collapsed_note: [],
      no_total_reason: {},
      as_of: { t: 1 },
      tiles: [{ label: "Units", value: 5, format: { f: 1 }, currency: {} }],
      row_flags: ["diff", { x: 1 }],
      totals: [null, "5"],
      control_result_ids: [1, "r2"],
    }) as ResultCardData;
    expect(card).not.toBeNull();
    expect(card.kind).toBe("table");
    expect(card.source).toBe("");
    expect(card.headline).toBeNull();
    expect(card.detail).toBeNull();
    expect(card.check).toBeNull();
    expect(card.share).toBeNull();
    expect(card.totals_label).toBeNull();
    expect(card.more_label).toBeNull();
    expect(card.as_of).toBeNull();
    expect(card.tiles).toEqual([{ label: "Units", value: 5, format: "integer", currency: null }]);
    expect(card.row_flags).toEqual(["diff"]);
    expect(card.totals).toBeNull();
    expect(card.control_result_ids).toEqual(["r2"]);
    expect(() => render(<ResultCard card={card} />)).not.toThrow();
  });

  it("R2: a step the backend recorded as failed is failed, whatever its message says", () => {
    const step = (summary: string, error?: boolean) =>
      ({ tool: "netsuite_suiteql", params: {}, result_summary: summary, duration_ms: 5, error }) as unknown as ToolCallStep;
    const [flagged, legacy, ok] = activityStepsFromCalls([
      step("Rows could not be read", true),
      step("Error: access denied"),
      step("Found 3 docs about failed logins"),
    ]);
    expect(flagged.status).toBe("error"); // the flag wins over any wording
    expect(legacy.status).toBe("error"); // old messages without the flag
    expect(ok.status).toBe("complete");
  });

  it("R3: a tab, newline or carriage return inside a cell can never start a new cell or record", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const card = coerceResultCard({ ...base, rows: [["Acme\t=1+1", 5], ["Beta\r\n=2+2", 6]] }) as ResultCardData;
    render(<ResultCard card={card} />);
    fireEvent.click(screen.getByRole("button", { name: /copy/i }));
    const text = (writeText.mock.calls[0] as unknown as [string])[0];
    const cells = text.split("\n").flatMap((line) => line.split("\t"));
    expect(cells.some((cell) => /^[=+\-@]/.test(cell))).toBe(false);
    expect(text.split("\n")).toHaveLength(3);
  });

  it("R4: SQL the agent ran stays collapsed while the answer is still streaming", () => {
    const blocks: StreamBlock[] = [
      {
        type: "tool",
        id: "t1",
        tool: { tool_name: "netsuite_suiteql", tool_input: {}, step: 1, status: "complete", duration_ms: 900 },
      },
      { type: "text", id: "x1", content: "Here it is.\n\n```sql\nSELECT country FROM transaction\n```\n" },
    ];
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MessageList messages={[]} isLoading={false} isWaitingForReply streamBlocks={blocks} />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("collapsed-sql")).toBeInTheDocument();
    expect(screen.queryByText(/SELECT country FROM transaction/)).not.toBeInTheDocument();
  });
});

describe("packet review round 2 on #370", () => {
  it("R1: a cell that is an object or array is turned into text before anything renders it", () => {
    const card = coerceResultCard({
      ...base,
      rows: [[{ toString: "source data" }, 5], [["a", "b"], { n: 1 }]],
    }) as ResultCardData;
    for (const row of card.rows) {
      for (const cell of row) expect(["string", "number", "boolean"].includes(typeof cell) || cell === null).toBe(true);
    }
    expect(() => render(<ResultCard card={card} />)).not.toThrow();
  });

  it("R2: a failed presentation step stays in the row and is counted as failed", () => {
    const steps = activityStepsFromCalls([
      { tool: "netsuite_suiteql", params: {}, result_summary: "20 rows", duration_ms: 5 },
      { tool: "compare_results", params: {}, result_summary: "Result r4 is unavailable.", duration_ms: 1, error: true },
      { tool: "present_result", params: {}, result_summary: "Card shown", duration_ms: 1 },
    ] as unknown as ToolCallStep[]);
    expect(steps.map((s) => [s.tool, s.status])).toEqual([
      ["netsuite_suiteql", "complete"],
      ["compare_results", "error"],
    ]);
  });

  it("R2: a presentation step that fails while streaming stays in the row", () => {
    const steps = activityStepsFromStream([
      { tool_name: "present_result", tool_input: {}, step: 2, status: "error" },
      { tool_name: "present_result", tool_input: {}, step: 3, status: "complete" },
    ]);
    expect(steps.map((s) => s.status)).toEqual(["error"]);
  });

  it("R3 (round 3): column metadata of the wrong type cannot crash rendering", () => {
    const card = coerceResultCard({
      ...base,
      columns: [{ key: { toString: "source data" } }, { key: "units", label: { x: 1 }, currency: {}, group: [] }],
    }) as ResultCardData;
    expect(card.columns.map((c) => [c.key, c.label])).toEqual([
      ["c0", ""],
      ["units", "units"],
    ]);
    expect(() => render(<ResultCard card={card} />)).not.toThrow();
  });

  it("R1 (round 3): a total in the first column is shown, with the label kept", () => {
    const card = coerceResultCard({
      ...base,
      columns: [
        { key: "orders", label: "Orders", format: "integer", align: "right" },
        { key: "country", label: "Country", format: "text", align: "left" },
      ],
      rows: [[12, "US"]],
      totals: [12, null],
      totals_label: "Total · 1 country",
    }) as ResultCardData;
    render(<ResultCard card={card} />);
    const total = screen.getByTestId("result-card-total");
    expect(total).toHaveTextContent("Total · 1 country");
    expect(total).toHaveTextContent("12");
  });

  it("R2 (round 3): saved failures without the flag are read from the backend's own wording", () => {
    const step = (summary: string) =>
      ({ tool: "netsuite_suiteql", params: {}, result_summary: summary, duration_ms: 5 }) as unknown as ToolCallStep;
    const statuses = activityStepsFromCalls(
      [
        "NetSuite request failed: timeout",
        "NetSuite API error 403: role lacks permission",
        "Query failed",
        "Permission denied: workspace.manage required",
        "connection_unavailable",
        "Found 3 docs about failed logins",
        "20 rows returned",
      ].map(step),
    ).map((s) => s.status);
    expect(statuses).toEqual(["error", "error", "error", "error", "error", "complete", "complete"]);
  });

  it("R4 (round 3): an open card shows why its rows have no total", () => {
    const card = coerceResultCard({ ...base, no_total_reason: "orders can sit in more than one country" }) as ResultCardData;
    render(<ResultCard card={card} />);
    expect(screen.getByText(/No total: orders can sit in more than one country/)).toBeInTheDocument();
  });

  it("R5 (round 3): a comparison never calls an older result 'as of' the newer one", () => {
    const comparison = {
      ...base,
      kind: "comparison",
      subtitle: "Matched on country",
      as_of: "2026-10-01T05:07:03+00:00",
      as_of_sources: [
        { label: "NetSuite", as_of: "2026-09-30T08:00:00+00:00" },
        { label: "Metabase", as_of: "2026-10-01T05:07:03+00:00" },
      ],
    };
    const { unmount } = render(<ResultCard card={coerceResultCard(comparison) as ResultCardData} />);
    expect(screen.queryByText(/both as of/)).not.toBeInTheDocument();
    expect(screen.getByText(/NetSuite as of .* · Metabase as of/)).toBeInTheDocument();
    unmount();
    const same = { ...comparison, as_of_sources: [comparison.as_of_sources[1], { label: "NetSuite", as_of: "2026-10-01T05:02:38+00:00" }] };
    render(<ResultCard card={coerceResultCard(same) as ResultCardData} />);
    expect(screen.getByText(/both as of/)).toBeInTheDocument();
  });

  it("R1 (round 4): a malformed Metabase join cannot crash the progress row", () => {
    const steps = activityStepsFromCalls([
      {
        tool: "ext__6f95665c23cc4d35a3f9eb4231099568__query",
        params: { query: { "lib/type": "mbql/query", stages: [{ "source-table": ["db", "public", "orders"], joins: [null, 5, { stages: [null] }] }] } },
        result_summary: "Query failed",
        duration_ms: 5,
      },
    ] as unknown as ToolCallStep[]);
    expect(() => render(<ToolActivityRow steps={steps} elapsedMs={1000} />)).not.toThrow();
    fireEvent.click(screen.getByTestId("tool-activity-done"));
    expect(screen.getByTestId("tool-activity-steps")).toBeInTheDocument();
  });

  it("R2 (round 4): a formula wrapped in quotes is neutralised on Copy", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const card = coerceResultCard({ ...base, rows: [['"=1+1"', 5], [" '=2+2", 6]] }) as ResultCardData;
    render(<ResultCard card={card} />);
    fireEvent.click(screen.getByRole("button", { name: /copy/i }));
    const text = (writeText.mock.calls[0] as unknown as [string])[0];
    const cells = text.split("\n").slice(1).map((line) => line.split("\t")[0]);
    expect(cells.every((cell) => cell.startsWith("'"))).toBe(true);
  });

  it("R3 (round 4): Excel export keeps each column's own format", () => {
    exportToExcel.mockClear();
    const card = coerceResultCard({
      ...base,
      columns: [
        { key: "country", label: "Country", format: "text", align: "left" },
        { key: "rate", label: "Shipping rate", format: "currency", currency: "USD", align: "right" },
        { key: "share", label: "Share", format: "percent", align: "right" },
      ],
      rows: [["US", 25, 72.1]],
    }) as ResultCardData;
    render(<ResultCard card={card} />);
    fireEvent.click(screen.getByRole("button", { name: /excel/i }));
    expect(exportToExcel.mock.calls[0][0].columnTypes).toEqual({
      Country: "text",
      "Shipping rate": "currency",
      Share: "percent",
    });
  });

  it("R3: an array cell holding a formula is exported as quoted text", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const card = coerceResultCard({ ...base, rows: [[['=HYPERLINK("https://example.com","Open")'], 5]] }) as ResultCardData;
    render(<ResultCard card={card} />);
    fireEvent.click(screen.getByRole("button", { name: /copy/i }));
    const text = (writeText.mock.calls[0] as unknown as [string])[0];
    const cells = text.split("\n").flatMap((line) => line.split("\t"));
    expect(cells.some((cell) => /^[=+\-@]/.test(cell.trimStart()))).toBe(false);
  });
});
