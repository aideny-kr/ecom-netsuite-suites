import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { WriteConfirmationData } from "@/lib/types";
import { WriteConfirmationCard } from "../write-confirmation-card";

const card: WriteConfirmationData = {
  type: "write_confirmation", mutation_type: "create", record_type: "creditmemo", record_id: null,
  proposed_fields: { memo: "R231821517 reseller discount", tranDate: "2026-10-07" },
  current_record: null, tool_name: "ext__x__ns_createRecord", tool_input: {}, confirmation_token: "signed", status: "pending",
  target_account: "6738075", target_environment: "PRODUCTION",
  accounting_review: {
    kind: "credit_creation", order_reference: "R231821517", record_id: "16029044", case_id: "case", invoice_id: "16029044",
    before: { tranId: "INV371382", total: "13494.75" }, proposed_fields: { tranDate: "2026-10-07", memo: "R231821517 reseller discount" },
    scope: { netsuite_account_id: "6738075", subsidiary_id: "1" }, period: { id: "173" }, ar_account: "119", accounting_book: "1",
    approval_basis: "Create a credit memo for 674.73 USD.", source: { currency: "USD" }, memo: "R231821517 reseller discount",
    lines: [{ item_id: "1471", amount: "674.73" }],
    expected_after: { total: "674.73", subtotal: "674.73", taxTotal: "0.00" },
    expected_ledger: { debit: { "774": "674.73" }, credit: { "119": "674.73" } },
    balance: {
      before: { gross: "13494.75", net: "13494.75", tax: "0.00" },
      after: { gross: "12820.02", net: "12820.02", tax: "0.00" },
      source: { gross: "12820.02", net: "12820.02", tax: "0.00" },
    },
    sales_adjustment_account: "774", tax_account: "210",
  },
};

describe("Agent-proposed credit approval", () => {
  it("shows the order balance before, after and against the source, and requires acknowledgment", () => {
    const approve = vi.fn();
    render(<WriteConfirmationCard data={card} onConfirm={approve} onReject={vi.fn()} />);
    const table = screen.getByRole("table", { name: "Order balance" });
    expect(within(table).getByText("$13,494.75")).toBeVisible();
    expect(within(table).getAllByText("$12,820.02")).toHaveLength(2);
    expect(screen.getByText("R231821517 reseller discount")).toBeVisible();
    const button = screen.getByRole("button", { name: "Approve $674.73 credit and application" });
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(button);
    expect(approve).toHaveBeenCalled();
  });

  it("names the credit once verified and never offers a second write", () => {
    render(
      <WriteConfirmationCard
        data={{ ...card, status: "approved", accounting_verification: { status: "verified", credit_memo_id: "16123312", resolution: { credit_memo_number: "CM12127" } } }}
        onConfirm={vi.fn()} onReject={vi.fn()}
      />,
    );
    expect(screen.getByText("Credit CM12127 applied")).toBeVisible();
    expect(screen.getByText("Executed · verified")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
  });
});
