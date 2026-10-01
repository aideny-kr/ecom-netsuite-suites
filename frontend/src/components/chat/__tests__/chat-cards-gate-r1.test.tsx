// T2 gate round 1 on #370 (wf_00ac938c-123): one regression per confirmed or plausible finding.
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import React from "react";
import { proseSegments } from "../answer-layout";
import { ToolActivityRow, activityStepsFromCalls } from "../tool-activity-row";
import { ResultCard } from "../result-card";
import { coerceResultCard, tableCoveredByCards } from "@/lib/chat-stream";
import type { ResultCardData } from "@/lib/chat-stream";
import type { ToolCallStep } from "@/lib/types";

vi.mock("@/hooks/use-excel-export", () => ({
  useExcelExport: () => ({ exportToExcel: vi.fn(), exportFromQuery: vi.fn(), isExporting: false }),
}));
vi.mock("@/hooks/use-saved-queries", () => ({ useCreateSavedQuery: () => ({ mutate: vi.fn(), isPending: false }) }));

describe("gate round 1 on #370", () => {
  it("opens a Metabase step (MBQL object query) without crashing", () => {
    const step = {
      tool: "ext__6f95665c23cc4d35a3f9eb4231099568__query",
      params: { query: { "lib/type": "mbql/query", stages: [{ "source-table": ["db", "public", "spree_orders"] }] } },
      result_summary: "Returned 1 row",
      result_payload: { kind: "table", columns: ["a"], rows: [[1]], row_count: 1, truncated: false, query: "", limit: 1 },
      duration_ms: 1800,
    } as unknown as ToolCallStep;
    render(<ToolActivityRow steps={activityStepsFromCalls([step])} elapsedMs={2000} />);
    fireEvent.click(screen.getByTestId("tool-activity-done"));
    // Opening renders the table result in the query card (round 2 on #370); the MBQL object is
    // never rendered as text, so there is no query toggle and no crash.
    fireEvent.click(screen.getByText(/Looked up order/));
    expect(screen.getByTestId("tool-activity-steps")).toBeInTheDocument();
    expect(screen.queryByText(/Show query/i)).not.toBeInTheDocument();
  });

  it("marks a step failed only when its summary states a failure", () => {
    const make = (summary: string) =>
      ({ tool: "rag_search", params: {}, result_summary: summary, duration_ms: 5 }) as unknown as ToolCallStep;
    const [ok, failed] = activityStepsFromCalls([make("Found 3 docs about failed logins"), make("Error: access denied")]);
    expect(ok.status).toBe("complete");
    expect(failed.status).toBe("error");
  });

  it("hides only the raw tables a card presents", () => {
    const cards = [{ result_ids: ["r9"] }] as unknown as ResultCardData[];
    expect(tableCoveredByCards({ result_id: "r9" }, cards)).toBe(true);
    expect(tableCoveredByCards({ result_id: "r2" }, cards)).toBe(false);
    expect(tableCoveredByCards({}, cards)).toBe(false);
  });

  it("keeps a fenced code block whole when splitting the answer", () => {
    const text = "Lead.\n\n```sql\nSELECT 1\n\n| a | b |\n| - | - |\n```\n\nAfter.";
    const segments = proseSegments(text);
    expect(segments).toHaveLength(1);
    expect(segments[0].kind).toBe("prose");
  });

  it("degrades a malformed card instead of crashing", () => {
    const card = coerceResultCard({
      card_id: "c",
      kind: "table",
      title: "t",
      source: "NetSuite",
      columns: [
        { key: "k", label: "K", format: "text", align: "left" },
        { key: "v", label: "V", format: "integer", align: "right" },
      ],
      rows: [["a", 1], null, ["b"]],
      share: { label: "Share", of: 1, values: [50] },
      totals: [null],
      tiles: [],
      top_n: 5,
      collapsed: false,
    })!;
    expect(card.rows).toEqual([["a", 1], ["b", null]]);
    expect(card.share).toBeNull();
    expect(card.totals).toBeNull();
    render(<ResultCard card={card} />);
    expect(screen.getAllByTestId("result-card-row")).toHaveLength(2);
  });

  it("never styles a blank difference as a flagged one", () => {
    const card = coerceResultCard({
      card_id: "c",
      kind: "comparison",
      result_ids: ["r1", "r2"],
      title: "t",
      source: "A vs B",
      columns: [
        { key: "key", label: "K", format: "text", align: "left" },
        { key: "d", label: "Difference", format: "delta", align: "right" },
      ],
      rows: [["x", ""]],
      tiles: [],
      top_n: 5,
      collapsed: false,
    })!;
    render(<ResultCard card={card} />);
    const cell = screen.getAllByTestId("result-card-row")[0].lastElementChild!;
    expect(cell.className).not.toContain("text-amber");
  });
});

describe("gate round 2 on #370", () => {
  it("never hides a table whose caveats the card does not carry", () => {
    const cards = [{ result_ids: ["r9"] }] as unknown as ResultCardData[];
    expect(tableCoveredByCards({ result_id: "r9", caveats: ["Snapshot is 3 days old"] } as never, cards)).toBe(false);
  });

  it("flags a failed step on the collapsed row", () => {
    const steps = activityStepsFromCalls([
      { tool: "netsuite_suiteql", params: { query: "SELECT 1" }, result_summary: "Error: timeout", duration_ms: 5 },
    ] as unknown as ToolCallStep[]);
    render(<ToolActivityRow steps={steps} elapsedMs={1000} />);
    expect(screen.getByTestId("tool-activity-done")).toHaveTextContent("1 failed");
  });

  it("keeps share values aligned with the rows that survive", () => {
    const card = coerceResultCard({
      card_id: "c",
      kind: "table",
      title: "t",
      source: "NetSuite",
      columns: [
        { key: "k", label: "K", format: "text", align: "left" },
        { key: "v", label: "V", format: "integer", align: "right" },
      ],
      rows: [["a", 1], null, ["b", 3]],
      share: { label: "Share", of: 1, values: [25, 0, 75] },
      tiles: [null, { label: "Orders", value: "x" }, { label: "Units", value: 4, format: "integer" }],
      queries: [null, { label: "SuiteQL query", text: "SELECT 1" }],
      top_n: 5,
      collapsed: false,
    })!;
    expect(card.share!.values).toEqual([25, 75]);
    expect(card.tiles.map((t) => t.label)).toEqual(["Units"]);
    expect(card.queries).toHaveLength(1);
  });
});

describe("answer-layout label stripping (round 2 on #370)", () => {
  it("keeps a sentence that merely starts with 'Query' before SQL", async () => {
    const { stripRanQueryLabels } = await import("../answer-layout");
    const warning = "Query failed on the first try, here is the fix:\n```sql\nSELECT 1\n```";
    expect(stripRanQueryLabels(warning)).toBe(warning);
    expect(stripRanQueryLabels("**Query I ran (SuiteQL):**\n```sql\nSELECT 1\n```")).toBe("```sql\nSELECT 1\n```");
  });
});
