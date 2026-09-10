import React from "react";
import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { deltaValue, Variance } from "./variance";

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
