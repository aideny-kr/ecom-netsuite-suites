import { describe, expect, it, vi } from "vitest";
import { approveExactBatch } from "./bulk-approval";
import type { TransactionProposal } from "../transaction-ops/types";
const proposal = (id: string) =>
  ({
    id,
    status: "pending",
    evidence_fingerprint: `hash-${id}`,
    valid_until: new Date(Date.now() + 60000).toISOString(),
  }) as TransactionProposal;
describe("exact bulk approval", () => {
  it("uses each approved fingerprint and stops on uncertain failure without retrying", async () => {
    const rows = [proposal("a"), proposal("b"), proposal("c")];
    const decide = vi
      .fn()
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce(new Error("Connection interrupted"));
    const results = await approveExactBatch(
      rows,
      () => rows,
      decide,
      () => true,
    );
    expect(decide).toHaveBeenCalledTimes(2);
    expect(decide.mock.calls[0][0]).toEqual({
      id: "a",
      decision: "approve",
      evidence_fingerprint: "hash-a",
    });
    expect(results.map((r) => r.status)).toEqual([
      "approved",
      "unconfirmed",
      "not_submitted",
    ]);
  });
  it("rejects a changed or expired selection before submitting any decision", async () => {
    const rows = [proposal("a"), proposal("b")];
    const decide = vi.fn();
    await expect(
      approveExactBatch(
        rows,
        () => [rows[0], { ...rows[1], evidence_fingerprint: "changed" }],
        decide,
        () => true,
      ),
    ).rejects.toThrow("changed");
    expect(decide).not.toHaveBeenCalled();
    rows[0].valid_until = new Date(0).toISOString();
    await expect(
      approveExactBatch(
        rows,
        () => rows,
        decide,
        () => true,
      ),
    ).rejects.toThrow("expired");
  });
  it("stops remaining approvals if access changes mid-batch", async () => {
    const rows = [proposal("a"), proposal("b")];
    let allowed = true;
    const decide = vi.fn(async () => {
      allowed = false;
    });
    const result = await approveExactBatch(
      rows,
      () => rows,
      decide,
      () => allowed,
    );
    expect(decide).toHaveBeenCalledTimes(1);
    expect(result[1].status).toBe("not_submitted");
  });
});
