import React from "react";
import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import { ComparisonEvidence } from "./evidence";

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
