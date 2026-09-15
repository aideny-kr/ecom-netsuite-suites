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
  it("polls a durable group after the chat ends and follows child verification", () => {
    const group = {
      accounting_group: { members: [{ card }] }, status: "executing",
      accounting_group_dispatch: { status: "queued", next_at: new Date(now).toISOString() },
    };
    expect(accountingProgressPending([{ structured_output: group }], now)).toBe(true);
    const finished = { ...group, accounting_group_dispatch: { ...group.accounting_group_dispatch, status: "finished" } };
    expect(accountingProgressPending([{ structured_output: finished }], now)).toBe(true);
    const verified = { ...finished, accounting_group: { members: [{ card: { ...card, accounting_completion: { status: "done" } } }] } };
    expect(accountingProgressPending([{ structured_output: verified }], now)).toBe(false);
    expect(accountingProgressPending([{ structured_output: group }], now + 31 * 60 * 1000)).toBe(false);
  });
});
