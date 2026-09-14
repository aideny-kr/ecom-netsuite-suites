import { describe, expect, it } from "vitest";
import { accountingProgressPending } from "../accounting-progress";

describe("accounting background results", () => {
  const now = Date.now();
  const card = {
    accounting_review: {}, accounting_execution: { accepted_at: new Date(now).toISOString() },
    accounting_recheck: { status: "queued" }, status: "approved",
  };
  it("polls a verified correction until its delayed reconciliation receipt arrives", () => {
    expect(accountingProgressPending([{ structured_output: card }], now)).toBe(true);
    for (const status of ["done", "blocked"]) {
      expect(accountingProgressPending([{ structured_output: { ...card, accounting_completion: { status } } }], now)).toBe(false);
    }
  });
  it("does not keep polling historical results or unapproved cards", () => {
    expect(accountingProgressPending([{ structured_output: card }], now + 31 * 60 * 1000)).toBe(false);
    expect(accountingProgressPending([{ structured_output: { accounting_review: {}, status: "pending" } }], now)).toBe(false);
  });
});
