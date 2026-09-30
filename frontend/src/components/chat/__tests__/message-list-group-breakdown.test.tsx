import { render, screen } from "@testing-library/react";
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

const breakdown = {
  group_id: "g",
  case_id: null,
  scope: null,
  pattern: "Order differences",
  currency: "USD",
  orders: 1,
  totals: { order_total: "-59.00", tax: "0.00", refunds: "0.00" },
  causes: [
    {
      cause: "source_adjustment_not_in_netsuite",
      label: "Solidus adjustment never reached NetSuite",
      why: "…",
      next_step: "fix_at_source",
      next_pill: "Fix at the source",
      next_label: "…",
      orders: 1,
      order_references: ["R290684941"],
      amounts: { order_total: "-59.00", tax: "0.00", refunds: "0.00" },
      primary: { metric: "order_total", amount: "-59.00" },
      facts: [],
    },
  ],
  checked: { saved_evidence: 1, saved_source_orders: 1, netsuite: "not_needed", netsuite_orders: 0, seconds: 0.4 },
};

function show(structured_output: Record<string, unknown>) {
  const messages = [
    { id: "u", role: "user", content: "Explain the group", created_at: "2026-09-29T17:00:00Z" },
    { id: "a", role: "assistant", content: "Four situations.", structured_output, created_at: "2026-09-29T17:00:01Z" },
  ];
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MessageList
        messages={messages as unknown as Parameters<typeof MessageList>[0]["messages"]}
        isLoading={false}
      />
    </QueryClientProvider>,
  );
}

describe("MessageList — saved group breakdown", () => {
  it("shows the card saved as the turn's output", () => {
    show({ type: "group_breakdown", data: breakdown });
    expect(screen.getByText("Solidus adjustment never reached NetSuite")).toBeInTheDocument();
  });

  it("keeps the card when a later tool in the same turn became the saved output", () => {
    // Review round 3 of #356: the breakdown is saved under its own key as well.
    show({ type: "data_table", data: { columns: [], rows: [] }, group_breakdown: breakdown });
    expect(screen.getByText("Solidus adjustment never reached NetSuite")).toBeInTheDocument();
  });
});
