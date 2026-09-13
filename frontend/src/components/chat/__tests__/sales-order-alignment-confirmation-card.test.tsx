import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { WriteConfirmationData, SalesOrderAlignmentReview } from "@/lib/types";
import { WriteConfirmationCard } from "../write-confirmation-card";
import { creditCard } from "./sales-credit.fixture";

const review: SalesOrderAlignmentReview = {
  ...(creditCard.accounting_review as Exclude<typeof creditCard.accounting_review, undefined> & { kind: "sales_adjustment_credit" }),
  kind: "sales_order_source_alignment", before: { total: "100", taxTotal: "0", amountPaid: "0", amountRemaining: "100", discountTotal: "0" },
  proposed_fields: { discountItem: { id: "50" }, discountRate: -5 },
  invoice_id: "20", support: { invoice: { total: "95" } },
  expected_after: { total: "95", subtotal: "100", taxTotal: "0", discountTotal: "-5" },
};
const card: WriteConfirmationData = { ...creditCard, mutation_type: "update", record_type: "salesorder", record_id: "20", accounting_review: review, proposed_fields: review.proposed_fields };
describe("Sales order alignment approval", () => {
  it("shows the exact order impact and requires acknowledgment", () => {
    const approve = vi.fn(); const original = JSON.stringify(card);
    render(<WriteConfirmationCard data={card} onConfirm={approve} onReject={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Align sales order with source" })).toBeVisible();
    expect(screen.getByRole("row", { name: "Sales order total $100.00 $95.00" })).toBeVisible();
    expect(screen.getByRole("row", { name: "Sales Adjustment $0.00 -$5.00" })).toBeVisible();
    const button = screen.getByRole("button", { name: "Approve sales-order amendment" });
    expect(button).toBeDisabled(); fireEvent.click(screen.getByRole("checkbox")); fireEvent.click(button);
    expect(approve).toHaveBeenCalledWith({}); expect(JSON.stringify(card)).toBe(original);
  });
  it.each(["indeterminate", "executing", "approved", "failed", "rejected"] as const)("does not offer another write when %s", status => {
    render(<WriteConfirmationCard data={{ ...card, status }} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
  });
  it("preserves backend blocks", () => {
    render(<WriteConfirmationCard data={{ ...card, invariant_errors: ["Period locked"] }} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByRole("checkbox")).toBeDisabled(); expect(screen.getByRole("alert")).toBeVisible();
  });
});
