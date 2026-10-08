import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import React from "react";
import { ResultCard, ResultCardHeadline, formatCardValue } from "../result-card";
import type { ResultCardData } from "@/lib/chat-stream";

vi.mock("@/hooks/use-excel-export", () => ({
  useExcelExport: () => ({ exportToExcel: vi.fn(), exportFromQuery: vi.fn(), isExporting: false }),
}));

const measure = { align: "right" as const, currency: null };
// compare_results output for the Yucca NetSuite vs Metabase turn.
const comparison: ResultCardData = {
  card_id: "card-2",
  kind: "comparison",
  result_ids: ["r9", "r21"],
  title: "Yucca orders by ship country · NetSuite vs Metabase",
  source: "NetSuite vs Metabase",
  subtitle: "Matched on country name",
  as_of: "2026-10-01T05:07:03+00:00",
  queries: [
    { label: "NetSuite query (SuiteQL)", text: "SELECT 1" },
    { label: "Metabase query", text: "From public.spree_line_items" },
  ],
  columns: [
    { key: "key", label: "Country", format: "text", align: "left" },
    { key: "m0_left", label: "NetSuite", format: "integer", group: "Orders", ...measure },
    { key: "m0_right", label: "Metabase", format: "integer", group: "Orders", ...measure },
    { key: "m1_left", label: "NetSuite", format: "integer", group: "Units", ...measure },
    { key: "m1_right", label: "Metabase", format: "integer", group: "Units", ...measure },
    { key: "m1_delta", label: "Difference", format: "delta", group: "Units", ...measure },
  ],
  rows: [
    ["United States", 162, 162, 170, 166, -4],
    ["Canada", 17, 17, 17, 17, 0],
    ["Sweden", 2, 2, 2, 2, 0],
  ],
  row_flags: ["diff", null, null],
  totals: [null, 228, 228, 240, 232, -8],
  totals_label: "Total · 20 countries",
  check: { status: "ok", text: "In each source, the country rows add up to that source's overall total." },
  tiles: [],
  top_n: 2,
  more_label: "Show 1 more countries · identical in both sources",
  collapsed: false,
  headline: "Orders match NetSuite in every country. Units differ in 4 countries.",
  detail: "Metabase has 8 fewer units, all in United States, Germany, Switzerland and United Kingdom.",
};

describe("ResultCard — comparison", () => {
  it("groups each measure's columns and highlights rows that differ", () => {
    render(<ResultCard card={comparison} />);
    expect(screen.getByText("Orders")).toBeInTheDocument();
    expect(screen.getByText("Units")).toBeInTheDocument();
    const rows = screen.getAllByTestId("result-card-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveAttribute("data-flag", "diff");
    expect(rows[0]).toHaveTextContent("−4");
    expect(rows[1]).toHaveTextContent("—");
    expect(screen.getByTestId("result-card-total")).toHaveTextContent("−8");
    fireEvent.click(screen.getByTestId("result-card-more"));
    expect(screen.getAllByTestId("result-card-row")).toHaveLength(3);
    expect(screen.getAllByTestId("result-card-query-toggle")).toHaveLength(2);
  });

  it("states the server's headline", () => {
    render(<ResultCardHeadline card={comparison} />);
    expect(screen.getByTestId("result-card-headline")).toHaveTextContent(
      "Orders match NetSuite in every country. Units differ in 4 countries.",
    );
  });

  it("formats values by column format", () => {
    expect(formatCardValue(1147128, { format: "currency", currency: "USD" })).toBe("$1,147,128.00");
    expect(formatCardValue("162", { format: "integer" })).toBe("162");
    expect(formatCardValue(3, { format: "delta" })).toBe("+3");
    expect(formatCardValue(null, { format: "integer" })).toBe("—");
  });
});
