import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import React from "react";
import { ResultCard } from "../result-card";
import { coerceResultCard } from "@/lib/chat-stream";

vi.mock("@/hooks/use-excel-export", () => ({ useExcelExport: () => ({ exportToExcel: vi.fn(), isExporting: false }) }));

// Exactly what the backend's fix_blockers.card() builds for an order Solidus can still change (synthetic data).
const BLOCKER_CARD = {
  card_id: "fix-blocker:R123456789:source_not_final",
  kind: "table",
  result_ids: [],
  title: "Fix blocked: Solidus can still change this order",
  source: "Solidus",
  subtitle: "R123456789 · who acts: Solidus operations",
  as_of: "2026-10-27T19:47:00Z",
  scope: null,
  queries: [],
  columns: [
    { key: "id", label: "Adjustment", format: "text", align: "left" },
    { key: "label", label: "Label", format: "text", align: "left" },
    { key: "applies_to", label: "Applies to", format: "text", align: "left" },
    { key: "amount", label: "Amount", format: "currency", currency: "USD", align: "right" },
    { key: "created", label: "Created", format: "date", align: "left" },
  ],
  rows: [["11", "Manual Tax Correction", "order", "-47.04", "2026-10-21"]],
  check: {
    status: "warn",
    text:
      "Void each open adjustment below if it repeats a change already made, or finalize it. " +
      "Then ask again; the app never corrects NetSuite while Solidus can still change the order.",
  },
  tiles: [],
  top_n: 20,
  collapsed: false,
  truncated: false,
};

describe("fix blocker card", () => {
  it("renders who acts, the open adjustment with its amount, and what to do", () => {
    const card = coerceResultCard(BLOCKER_CARD);
    expect(card).not.toBeNull();
    render(<ResultCard card={card!} />);
    const el = screen.getByTestId("result-card");
    expect(within(el).getByText("Fix blocked: Solidus can still change this order")).toBeTruthy();
    expect(within(el).getByText(/who acts: Solidus operations/)).toBeTruthy();
    expect(within(el).getByText("Manual Tax Correction")).toBeTruthy();
    expect(within(el).getByText(/47\.04/)).toBeTruthy();
    expect(within(el).getByTestId("result-card-check").textContent).toContain("Void each open adjustment");
  });
});
