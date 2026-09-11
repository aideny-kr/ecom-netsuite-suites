import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { WriteConfirmationData } from "@/lib/types";
import { WriteConfirmationCard } from "../write-confirmation-card";

import { creditCard } from "./sales-credit.fixture";

function mount(data = creditCard) {
  const approve = vi.fn();
  const result = render(<WriteConfirmationCard data={data} onConfirm={approve} onReject={vi.fn()} />);
  return { ...result, approve };
}

describe("Sales Adjustments approval", () => {
  it("shows the approved financial bridge and requires acknowledgment without changing the signed request", () => {
    const original = JSON.stringify(creditCard);
    const { approve } = mount();
    const table = screen.getByRole("table", { name: "Invoice reconciliation" });
    expect(within(table).getByText("$106.00")).toBeVisible();
    expect(within(table).getByText("−$5.00")).toBeVisible();
    expect(within(table).getAllByText("$101.00")).toHaveLength(2);
    expect(within(table).getAllByText("$0.00")).toHaveLength(2);
    const button = screen.getByRole("button", { name: "Approve $5.00 credit and application" });
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(button);
    expect(approve).toHaveBeenCalledWith({});
    expect(JSON.stringify(creditCard)).toBe(original);
  });
  it.each(["indeterminate", "executing", "approved", "failed", "rejected"] as const)("never offers a second write for %s", status => {
    mount({ ...creditCard, status });
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(screen.queryByText("Executed · verified")).not.toBeInTheDocument();
    expect(screen.getByText("Proposed result")).toBeVisible();
  });
  it("keeps an unpaid invoice balance distinct from zero reconciliation variance", () => {
    const review = creditCard.accounting_review!;
    if (review.kind !== "sales_adjustment_credit") throw new Error("Expected credit fixture");
    mount({ ...creditCard, accounting_review: { ...review, expected_after: { ...review.expected_after, invoice_remaining: "101.00" } } });
    const balance = screen.getByText(/Remaining receivable; separate from reconciliation variance/);
    expect(balance).toHaveTextContent("$101.00");
    const table = screen.getByRole("table", { name: "Invoice reconciliation" });
    expect(within(table).getByRole("row", { name: "Remaining variance $0.00" })).toBeInTheDocument();
  });
  it("uses verified evidence and keeps bank clearance separate", () => {
    mount({ ...creditCard, status: "approved", accounting_verification: { status: "verified", credit_memo_id: "30", resolution: {
      credit_memo_number: "CM30", credit_amount: "5.00", tax_amount: "0.00", net_invoice_total: "101.00", remaining_variance: "0.00",
    } } });
    expect(screen.getByText("Executed · verified")).toBeVisible();
    expect(screen.getByText("Applied Sales Adjustments credit")).toBeVisible();
    expect(screen.getByText(/Bank and processor clearance remain separate/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
  });
  it.each([
    { status: "queued" as const, run_id: "recheck-run" },
    { status: "not_queued" as const, reason: "queue unavailable" },
  ])("distinguishes verified posting from the case recheck: $status", accounting_recheck => {
    mount({ ...creditCard, status: "approved", accounting_verification: { status: "verified" }, accounting_recheck });
    expect(screen.getByText("Executed · verified")).toBeVisible();
    if (accounting_recheck.status === "queued") {
      expect(screen.getByRole("link", { name: "View reconciliation result →" })).toHaveAttribute("href", "/transaction-operations/runs/recheck-run");
      expect(screen.getByText(/reconciliation was queued/)).toBeVisible();
    } else {
      expect(screen.getByText(/The case still needs review/)).toBeVisible();
      expect(screen.queryByRole("link", { name: /View reconciliation/ })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
  });
  it.each([
    { invariant_errors: ["Period closed"] },
    { unfillable_line_fields: ["Missing item"] },
    { editable_slots: [{ name: "account", label: "Account", type: "string" }] },
  ])("preserves backend blocks", gaps => {
    mount({ ...creditCard, ...gaps });
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Approve/ })).toBeDisabled();
    expect(screen.getByRole("checkbox")).toBeDisabled();
  });
  it("requires fresh acknowledgment when the proposal identity changes", () => {
    const { rerender } = mount();
    fireEvent.click(screen.getByRole("checkbox"));
    rerender(<WriteConfirmationCard data={{ ...creditCard, confirmation_token: "replacement" }} onConfirm={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("button", { name: /Approve/ })).toBeDisabled();
  });
  it("renders a credit group without tax-correction arithmetic or per-child approval", () => {
    mount({ ...creditCard, accounting_review: null, accounting_group: { group_id: "group", concurrency: 3,
      members: [{ case_id: "case", order_reference: "R123456789", confirmation_id: "child", card: creditCard }],
    } });
    expect(screen.getByText(/Sales Adjustments credit \$5.00 · Net invoice \$101.00 · Tax impact \$0.00/)).toBeVisible();
    expect(screen.queryByText(/NaN|tax-rate calculation issue/)).not.toBeInTheDocument();
    expect(screen.getAllByRole("checkbox")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Approve $5.00 credit and application" })).not.toBeInTheDocument();
  });
});
