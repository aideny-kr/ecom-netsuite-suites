import { describe, expect, it } from "vitest";
import { transactionAmount } from "./format";

describe("transaction amounts", () => {
  it("preserves cents above JavaScript's exact integer range", () => {
    expect(transactionAmount("999999999999999999.123456", "USD")).toBe("999,999,999,999,999,999.123456");
  });
  it("uses currency precision without rounding source evidence", () => {
    expect(transactionAmount("1724.000000", "USD")).toBe("1,724.00");
    expect(transactionAmount("1724.000000", "JPY")).toBe("1,724");
    expect(transactionAmount("12.345000", "KWD")).toBe("12.345");
    expect(transactionAmount("12.345600", "USD")).toBe("12.3456");
  });
  it("distinguishes unknown, zero and negative values", () => {
    expect(transactionAmount(null, "USD")).toBe("—");
    expect(transactionAmount("0.000000", "USD")).toBe("0.00");
    expect(transactionAmount("-124.200000", "USD")).toBe("(124.20)");
    expect(transactionAmount("invalid", "USD")).toBe("—");
  });
});
