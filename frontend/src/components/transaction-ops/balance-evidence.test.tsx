import React from "react";
import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import { ComparisonEvidence, FindingCard } from "./evidence";

it("explains why a safe proposal cannot yet be prepared", () => {
  render(
    <ComparisonEvidence
      report={{
        automation: { status: "blocked", code: "create_mapping_unproven" },
      }}
    />,
  );
  expect(
    screen.getByText("A solution needs more evidence"),
  ).toBeInTheDocument();
  expect(screen.getByText(/backend mapping.*verified/i)).toBeInTheDocument();
});

it("shows independently verified balances even when detailed repair evidence is incomplete", () => {
  render(
    <ComparisonEvidence
      report={{
        balance: {
          status: "incomplete",
          currency: "USD",
          amounts: {
            order_total: {
              source: "100.123456",
              target: "100.123456",
              delta: "0.000000",
            },
            tax: {
              source: "10.000000",
              target: "10.000000",
              delta: "0.000000",
            },
            refunds: { source: "0.00", target: null, delta: null },
          },
        },
        comparison: { recommended_action: "gather_evidence", differences: [] },
      }}
    />,
  );
  const table = screen.getByRole("table", { name: /Order reconciliation/ });
  expect(within(table).getAllByText("100.123456")).toHaveLength(2);
  const refunds = within(table).getByRole("row", { name: /Completed refunds/ });
  expect(within(refunds).getByText("0.00")).toBeInTheDocument();
  expect(within(refunds).getAllByText("Unknown")).toHaveLength(2);
});

const correctedFinding = {
  id: "finding-1", run_id: "run-1", order_reference: "R146850445",
  created_at: "2026-09-13T17:31:30Z", updated_at: "2026-09-13T17:31:30Z",
  report_json: { comparison: { currency: "USD", recommended_action: "gather_evidence" } },
};

it("shows a verified recheck outcome while preserving raw repair evidence", () => {
  render(<FindingCard finding={correctedFinding} accountingReconciliation={{
    status: "succeeded", finding_id: "finding-1", verification_scope: "order_total_tax_refunds",
  }} />);
  expect(screen.getByText("Matched after approved correction")).toBeInTheDocument();
  expect(screen.getByText("Raw record comparison and repair evidence")).toBeInTheDocument();
  expect(screen.getByText(/Cash settlement remains separate/)).toBeInTheDocument();
});

it.each([
  undefined,
  { status: "succeeded", finding_id: "another-finding", verification_scope: "order_total_tax_refunds" },
  { status: "unverified", finding_id: "finding-1", verification_scope: "order_total_tax_refunds" },
  { status: "succeeded", finding_id: "finding-1", verification_scope: "invoice_only" },
])("does not claim a match without the exact successful full recheck (%j)", (result) => {
  render(<FindingCard finding={correctedFinding} accountingReconciliation={result} />);
  expect(screen.getByText("More evidence needed")).toBeInTheDocument();
  expect(screen.queryByText("Matched after approved correction")).not.toBeInTheDocument();
});
