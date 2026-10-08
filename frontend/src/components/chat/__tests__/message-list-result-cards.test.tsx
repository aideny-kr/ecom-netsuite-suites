import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeAll, describe, expect, it, vi } from "vitest";
import React from "react";
import { MessageList } from "../message-list";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn(async () => ({})), post: vi.fn(async () => ({})) },
}));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => ({ user: null }) }));

beforeAll(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  (global as { ResizeObserver?: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

const countries = [
  ["United States", 162, 170, 1147128],
  ["Canada", 17, 17, 114865.28],
  ["Germany", 10, 11, 64793.3],
  ["Switzerland", 7, 9, 48408.45],
  ["Australia", 4, 4, 40752.72],
  ["United Kingdom", 5, 6, 28792.5],
  ["France", 4, 4, 25463.32],
  ["New Zealand", 2, 2, 13406.58],
  ["Norway", 2, 2, 13401.64],
];

// The card present_result builds for the Yucca "by country" turn (values from the server).
const card = {
  card_id: "card-1",
  kind: "table",
  result_ids: ["r9"],
  title: "Yucca orders by ship country",
  source: "NetSuite",
  subtitle: "open sales orders",
  as_of: "2026-10-01T05:02:38+00:00",
  scope: "Open sales orders dated Sep 30 or later · Yucca item lines only",
  queries: [{ label: "SuiteQL query", text: "SELECT BUILTIN.DF(sa.country) AS ship_country FROM transaction t" }],
  columns: [
    { key: "ship_country", label: "Country", format: "text", align: "left" },
    { key: "orders", label: "Orders", format: "integer", align: "right" },
    { key: "units", label: "Units", format: "integer", align: "right" },
    { key: "yucca_line_amount_usd", label: "Yucca line value", format: "currency", currency: "USD", align: "right" },
  ],
  rows: countries,
  share: { label: "Share of value", of: 3, values: [72.1, 7.2, 4.1, 3.0, 2.6, 1.8, 1.6, 0.8, 0.8] },
  totals: [null, 228, 240, 1591780.86],
  totals_label: "Total · 20 countries",
  check: { status: "ok", text: "The rows add up to the overall total." },
  tiles: [
    { label: "Orders", value: 228, format: "integer" },
    { label: "Yucca line value", value: 1591780.86, format: "currency", currency: "USD" },
  ],
  top_n: 7,
  more_label: "Show 2 more countries · 2 orders each",
  less_label: "Show fewer countries",
  collapsed: false,
};

const answer =
  "The United States carries most of the Yucca volume.\n\n**Query I ran (SuiteQL):**\n```sql\nSELECT 1\n```\n\n" +
  "```followups\nCompare with Metabase\nBreak down by SKU\n```";

function show(onFollowUp = vi.fn()) {
  const messages = [
    { id: "u", role: "user", content: "can you break them down by country?", created_at: "2026-10-01T05:02:02Z" },
    {
      id: "a",
      role: "assistant",
      content: answer,
      created_at: "2026-10-01T05:02:38Z",
      model_used: "claude-sonnet-5-5",
      provider_used: "anthropic",
      is_byok: true,
      citations: [{ type: "doc", title: "NetSuite Modules", snippet: "" }],
      tool_calls: [
        { tool: "netsuite_suiteql", params: { query: "SELECT 1" }, result_summary: "Returned 20 rows", duration_ms: 4200 },
        { tool: "present_result", params: {}, result_summary: "card", duration_ms: 3 },
      ],
      structured_output: { type: "data_table", data: { columns: ["x"], rows: [[1]] }, result_cards: [card] },
    },
  ];
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MessageList
        messages={messages as unknown as Parameters<typeof MessageList>[0]["messages"]}
        isLoading={false}
        onFollowUp={onFollowUp}
      />
    </QueryClientProvider>,
  );
  return onFollowUp;
}

describe("MessageList — result cards", () => {
  it("leads with the answer, then tiles and the card; the raw table is not repeated", () => {
    show();
    const layout = screen.getByTestId("answer-with-cards");
    const lead = within(layout).getByText("The United States carries most of the Yucca volume.");
    const cardEl = within(layout).getByTestId("result-card");
    expect(lead.compareDocumentPosition(cardEl) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByTestId("result-card-tiles")).toHaveTextContent("$1.59M");
    expect(screen.queryByText("Query Results")).not.toBeInTheDocument();
  });

  it("formats totals, shares and the reconciliation check from the server", () => {
    show();
    expect(screen.getByTestId("result-card-total")).toHaveTextContent("Total · 20 countries228240$1,591,780.86100%");
    expect(screen.getByText("72.1%")).toBeInTheDocument();
    // A passing check sits beside the total; the sentence is its accessible name.
    expect(screen.getByTestId("result-card-total-check")).toHaveAttribute("aria-label", "The rows add up to the overall total.");
    expect(screen.queryByTestId("result-card-check")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("result-card-row")).toHaveLength(7);
    fireEvent.click(screen.getByTestId("result-card-more"));
    expect(screen.getAllByTestId("result-card-row")).toHaveLength(9);
  });

  it("keeps SQL collapsed, both on the card and in the text", () => {
    show();
    expect(screen.queryByText(/BUILTIN\.DF/)).not.toBeInTheDocument();
    expect(screen.queryByText("Query I ran (SuiteQL):")).not.toBeInTheDocument();
    expect(screen.getByTestId("collapsed-sql")).toHaveTextContent("Show query");
    fireEvent.click(screen.getByTestId("result-card-query-toggle"));
    expect(screen.getByText(/BUILTIN\.DF/)).toBeInTheDocument();
  });

  it("shows one collapsed step row instead of a card per tool", () => {
    show();
    const done = screen.getByTestId("tool-activity-done");
    expect(done).toHaveTextContent("Queried NetSuite");
    expect(done).toHaveTextContent("1 query · 36 s");
    expect(screen.queryByTestId("tool-activity-steps")).not.toBeInTheDocument();
    fireEvent.click(done);
    expect(screen.getByTestId("tool-activity-steps")).toHaveTextContent("SuiteQL query");
  });

  it("offers follow-ups as buttons and hides sources behind a toggle", () => {
    const onFollowUp = show();
    expect(screen.queryByText("```followups")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Compare with Metabase" }));
    expect(onFollowUp).toHaveBeenCalledWith("Compare with Metabase");
    expect(screen.queryByText("NetSuite Modules")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("sources-toggle"));
    expect(screen.getByText("NetSuite Modules")).toBeInTheDocument();
  });
});
