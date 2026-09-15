import React from "react";
import { expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { deltaValue, Variance } from "./variance";

it("shows the posting amounts and keeps the non-posting order alignment separate", () => {
  render(<Variance balance={{amounts: {order_total: {delta: "-433.80"}}, posting_reconciliation: {
    basis: "verified_source_revision_and_owned_credit_refund",
    amounts: {
      net: {source: "3655.00", target: "3621.20", delta: "33.80"},
      tax: {source: "308.84", target: "342.64", delta: "-33.80"},
      order_total: {source: "3963.84", target: "3963.84", delta: "0.00"},
      refunds: {source: "433.80", target: "433.80", delta: "0.00"},
    },
    sales_order_alignment: {status: "required", amounts: {order_total: {delta: "-433.80"}, tax: {delta: "-33.80"}}},
  }}} />);
  const posting = within(screen.getByRole("table", {name: /Posting comparison/}));
  expect(posting.getByText("+33.80")).toBeInTheDocument();
  expect(posting.getByText("3621.20")).toBeInTheDocument();
  expect(posting.queryByText("-433.80")).not.toBeInTheDocument();
  expect(screen.getByText(/alignment needed/)).toBeInTheDocument();
  expect(screen.getByText("-433.80")).toBeInTheDocument();
});

it("preserves pennies, arbitrary precision and separate gross/tax amounts", () => {
  render(
    <Variance
      balance={{
        amounts: {
          order_total: { source: "999", delta: "0.01" },
          tax: { delta: "-0.01" },
          refunds: { delta: "123456789012345.123456" },
        },
      }}
    />,
  );
  for (const value of ["+0.01", "-0.01", "+123456789012345.123456"])
    expect(screen.getByText(value)).toBeInTheDocument();
  expect(screen.queryByText("999")).not.toBeInTheDocument();
});
it("distinguishes unknown from exact zero without rounding", () => {
  for (const value of [null, undefined, 0.01, "NaN", "Infinity", "", "bad"])
    expect(deltaValue(value)).toEqual({ text: "—", nonzero: false });
  for (const value of ["0", "-0.00", "0E-12"])
    expect(deltaValue(value)).toEqual({ text: "0.00", nonzero: false });
  expect(deltaValue("0.000000000001")).toEqual({
    text: "+0.000000000001",
    nonzero: true,
  });
});
