import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { AccountingResolutionPlan, WriteConfirmationData } from "@/lib/types";
import { AccountingOrderPlanCard } from "../accounting-order-plan-card";
import { creditCard } from "./sales-credit.fixture";

const plan: AccountingResolutionPlan = {
  version: 1, order_reference: "R123456789", currency: "USD", active_step: "posting", status: "awaiting_approval",
  steps: [
    { id: "posting", title: "Correct invoice", status: "awaiting_approval", depends_on: [], affects_gl: true, current_total: "100", target_total: "95" },
    { id: "sales_order", title: "Align sales order", status: "waiting", depends_on: ["posting"], affects_gl: false, current_total: null, target_total: "95" },
    { id: "reconcile", title: "Reconcile the complete order", status: "waiting", depends_on: ["posting", "sales_order"] },
  ],
};
const data: WriteConfirmationData = { ...creditCard, accounting_review: { ...creditCard.accounting_review!, resolution_plan: plan } };

it("shows dependencies and unknown amounts without expanding financial approval authority", () => {
  const approve = vi.fn();
  HTMLElement.prototype.scrollIntoView = vi.fn();
  render(<AccountingOrderPlanCard data={data}><button onClick={approve}>Exact signed approval</button></AccountingOrderPlanCard>);
  expect(screen.getAllByText("Waiting for earlier steps")).toHaveLength(2);
  expect(screen.getByText("Source consistency · non-posting")).toBeVisible();
  expect(within(screen.getByRole("table")).getAllByText("—")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "Review exact correction →" }));
  expect(approve).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Exact signed approval" }));
  expect(approve).toHaveBeenCalledTimes(1);
});

it("retains penny variances, partial status and both approval identities", () => {
  const original: NonNullable<WriteConfirmationData["accounting_receipt"]> = {
    status: "partially_resolved", summary: "Invoice corrected; SO remains different.", approved_by: { id: "alice", name: "Alice" }, approved_at: "2026-09-13T20:00:00Z",
    record_links: [], reconciliation_url: "/transaction-operations", audit_url: "/audit", completion_audit_id: "original", next_step: { status: "blocked", reasons: ["SO amount still differs"] },
  };
  const latest = { ...original, completion_audit_id: "latest", approved_by: { id: "bob", name: "Bob" }, plan: { ...plan, steps: plan.steps.map(s => ({ ...s, status: s.id === "posting" ? "verified" : "needs_review" })) }, balance: { amounts: { order_total: { source: "95.00", target: "95.01", delta: "-0.01" }, tax: { source: null, target: "0", delta: null } } } };
  render(<AccountingOrderPlanCard data={{ ...data, status: "approved", accounting_receipt: original }} receipt={latest}><span>Exact correction retained</span></AccountingOrderPlanCard>);
  expect(screen.getByText("Further review required")).toBeVisible();
  expect(screen.getByText("-0.01")).toBeVisible();
  expect(screen.getByText(/Original correction approved by Alice/)).toBeVisible();
  expect(screen.getByText(/Latest correction approved by Bob/)).toBeVisible();
  expect(screen.getByText("SO amount still differs")).toBeVisible();
  expect(screen.queryByText("Final reconciliation result")).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("does not show an unsent group child as ready for approval", () => {
  render(<AccountingOrderPlanCard data={data} groupState="indeterminate"><span>Recorded operation</span></AccountingOrderPlanCard>);
  expect(within(screen.getByRole("table")).getByText("Not submitted")).toBeVisible();
  expect(screen.queryByRole("button", { name: /Review exact/ })).not.toBeInTheDocument();
});

it("preserves legacy cards when no durable plan exists", () => {
  render(<AccountingOrderPlanCard data={creditCard}><span>Existing accounting card</span></AccountingOrderPlanCard>);
  expect(screen.getByText("Existing accounting card")).toBeVisible();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});
