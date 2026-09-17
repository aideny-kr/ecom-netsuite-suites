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
        accounting_receipt: { ...receipt, status: "partially_resolved", approved_by: { id: "alice", name: "Alice" }, completion_audit_id: "original-audit-A" } }, resolution_receipt: receipt }] },
  };
  render(<AccountingGroupCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
  expect(screen.getByText("1 / 1 orders reconciled · 0 need further review")).toBeVisible();
  const result = screen.getByLabelText("Verified accounting result");
  expect(within(result).getByText(/Finance reviewer/)).toBeInTheDocument();
  expect(within(result).getByText(/Original correction approved by Alice/)).toBeInTheDocument();
  expect(within(result).getByText(/Original audit reference: original-audit-A/)).toBeInTheDocument();
  expect(within(result).getByText(/Latest correction approved by Finance reviewer/)).toBeInTheDocument();
  expect(within(result).getByText("Audit reference: audit")).toBeInTheDocument();
  expect(within(result).getByRole("link", { name: "Sales order", hidden: true })).toHaveAttribute("href", receipt.record_links[0].url);
  expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
});


it("counts orders that were never prepared apart from the approved corrections", () => {
  // The run behind the September post-mortem: 54 selected, 31 prepared, 30 reconciled. The
  // old line read "30 / 54 orders reconciled · N need further review", which folded orders
  // that never got a correction into the review queue and could never reach a terminal
  // state. They are counted, and named, separately.
  const data: WriteConfirmationData = {
    ...creditCard, accounting_review: null, status: "approved",
    accounting_plan_progress: {
      orders: 54, prepared: 31, unprepared: 23, deadline: 23,
      results_ready: 30, reconciled: 30, remaining: 1, status: "in_progress",
      computed_at: "2026-09-17T03:00:00Z",
    },
    accounting_group: { group_id: "group", concurrency: 3, members: [] },
  };
  render(<AccountingGroupCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
  expect(
    screen.getByText(
      "30 / 31 approved corrections reconciled · 1 still to reconcile · 23 of 54 orders not prepared",
    ),
  ).toBeVisible();
});
