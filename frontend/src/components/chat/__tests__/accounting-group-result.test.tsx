import { render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { WriteConfirmationData } from "@/lib/types";
import { AccountingGroupCard } from "../accounting-group-card";
import { creditCard } from "./sales-credit.fixture";

it("shows full-order results, original approval and real links without another write button", () => {
  const receipt: NonNullable<WriteConfirmationData["accounting_receipt"]> = {
    status: "reconciled", summary: "Source and ERP amounts reconcile.",
    approved_by: { id: "actor", name: "Finance reviewer" }, approved_at: "2026-09-13T20:00:00Z",
    record_links: [{ label: "Sales order", url: "https://6738075.app.netsuite.com/app/accounting/transactions/salesord.nl?id=20", record_type: "salesorder", record_id: "20" }],
    reconciliation_url: "/transaction-operations/runs/recheck", audit_url: "/audit", completion_audit_id: "audit",
    next_step: { status: "complete" },
  };
  const data: WriteConfirmationData = {
    ...creditCard, accounting_review: null, status: "approved",
    accounting_plan_progress: { orders: 1, results_ready: 1, reconciled: 1, remaining: 0, status: "reconciled" },
    accounting_group: { group_id: "group", concurrency: 3, members: [{ case_id: "case", order_reference: "R100",
      confirmation_id: "child", card: { ...creditCard, status: "approved", accounting_verification: { status: "verified" },
        accounting_receipt: { ...receipt, status: "partially_resolved" } }, resolution_receipt: receipt }] },
  };
  render(<AccountingGroupCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
  expect(screen.getByText("1 / 1 orders reconciled · 0 need further review")).toBeVisible();
  const result = screen.getByLabelText("Verified accounting result");
  expect(within(result).getByText(/Finance reviewer/)).toBeInTheDocument();
  expect(within(result).getByRole("link", { name: "Sales order", hidden: true })).toHaveAttribute("href", receipt.record_links[0].url);
  expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
});
