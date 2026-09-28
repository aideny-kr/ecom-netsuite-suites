import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { WriteConfirmationData } from "@/lib/types";
import { AccountingGroupCard } from "../accounting-group-card";
import { creditCard } from "./sales-credit.fixture";

const reconciled: NonNullable<WriteConfirmationData["accounting_receipt"]> = {
  status: "reconciled", summary: "Source and ERP amounts reconcile.",
  approved_by: { id: "actor", name: "Finance reviewer" }, approved_at: "2026-09-27T03:09:00Z",
  record_links: [], reconciliation_url: "/transaction-operations/runs/recheck", audit_url: "/audit",
  completion_audit_id: "audit", next_step: { status: "complete" },
};

it("shows live progress while approved corrections are written", () => {
  const child = { ...creditCard, status: "approved" } as WriteConfirmationData;
  const data: WriteConfirmationData = {
    ...creditCard, accounting_review: null, status: "executing",
    accounting_group_dispatch: {
      version: 1, status: "running",
      members: { a: { status: "verified" }, b: { status: "dispatching" }, c: { status: "queued" } },
    },
    accounting_group: {
      group_id: "g", concurrency: 3,
      members: [
        { case_id: "1", order_reference: "R100", confirmation_id: "a", card: child, resolution_receipt: reconciled },
        { case_id: "2", order_reference: "R200", confirmation_id: "b", card: child },
        { case_id: "3", order_reference: "R300", confirmation_id: "c", card: child },
        { case_id: "4", order_reference: "R400", reason: "Waiting.", set_aside: "waiting on Solidus to finalize" },
      ],
    },
  };
  render(<AccountingGroupCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
  expect(screen.getByText("1 of 3 done")).toBeVisible();
  expect(screen.getByLabelText("Writing now")).toHaveTextContent("R200");
  expect(screen.getAllByText("Reconciled").length).toBeGreaterThan(0);
  expect(screen.getAllByText("Writing").length).toBeGreaterThan(0);
  expect(screen.getAllByText("Queued").length).toBeGreaterThan(0);
  expect(screen.getByText(/waiting on Solidus to finalize/)).toBeInTheDocument();
});

it("says the whole group is reconciled when it is", () => {
  const child = { ...creditCard, status: "approved" } as WriteConfirmationData;
  const receipt = reconciled;
  const data: WriteConfirmationData = {
    ...creditCard, accounting_review: null, status: "approved",
    accounting_group_dispatch: { version: 1, status: "finished", members: { a: { status: "verified" }, b: { status: "verified" } } },
    accounting_group: {
      group_id: "g", concurrency: 3,
      members: [
        { case_id: "1", order_reference: "R100", confirmation_id: "a", card: child, resolution_receipt: receipt },
        { case_id: "2", order_reference: "R200", confirmation_id: "b", card: child, resolution_receipt: receipt },
      ],
    },
  };
  render(<AccountingGroupCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} />);
  expect(screen.getByText("2 of 2 corrections reconciled")).toBeVisible();
});
