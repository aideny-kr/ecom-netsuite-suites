// Independent packet review round 1 on #370 (gpt-6-astra): one regression per finding.
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeAll, describe, expect, it, vi } from "vitest";
import React from "react";
import { MessageList } from "../message-list";
import { ResultCard } from "../result-card";
import { activityStepsFromCalls } from "../tool-activity-row";
import { coerceResultCard } from "@/lib/chat-stream";
import type { ResultCardData, StreamBlock } from "@/lib/chat-stream";
import type { ToolCallStep } from "@/lib/types";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn(async () => ({})), post: vi.fn(async () => ({})) },
}));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => ({ user: null }) }));
vi.mock("@/hooks/use-excel-export", () => ({
  useExcelExport: () => ({ exportToExcel: vi.fn(), exportFromQuery: vi.fn(), isExporting: false }),
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
      step("NetSuite request failed: timeout", true),
      step("Error: access denied"),
      step("NetSuite request failed: timeout"),
    ]);
    expect(flagged.status).toBe("error");
    expect(legacy.status).toBe("error"); // old messages without the flag
    expect(ok.status).toBe("complete"); // without the flag the wording alone is not trusted beyond the prefix
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
