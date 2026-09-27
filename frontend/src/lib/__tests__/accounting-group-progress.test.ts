import { describe, expect, it } from "vitest";
import { groupProgress, liveState } from "../accounting-group-progress";

const card = { status: "approved" } as never;
const member = (id: string, extra: Record<string, unknown> = {}) => ({
  case_id: `case-${id}`, order_reference: `R${id}`, confirmation_id: `c-${id}`, card, ...extra,
});

describe("live group progress", () => {
  // 2026-09-27: the card counted "verified" from each order's preparation-time snapshot, so it
  // read "0 verified" for the whole run while the dispatch record said 18 were written.
  it("reads each order's state from its receipt first, then the live dispatch record", () => {
    const dispatch = { members: { "c-1": { status: "verified" }, "c-2": { status: "dispatching" }, "c-3": { status: "queued" }, "c-4": { status: "rejected" }, "c-5": { status: "verification_pending" } } };
    expect(liveState(member("1"), dispatch)).toBe("rechecking");
    expect(liveState(member("2"), dispatch)).toBe("writing");
    expect(liveState(member("3"), dispatch)).toBe("queued");
    expect(liveState(member("4"), dispatch)).toBe("refused");
    expect(liveState(member("5"), dispatch)).toBe("needs_review");
    expect(liveState(member("1", { resolution_receipt: { status: "reconciled" } }), dispatch)).toBe("reconciled");
    expect(liveState(member("6"), dispatch)).toBeNull();
    expect(liveState({ case_id: "x", order_reference: "R9" }, dispatch)).toBeNull();
  });

  it("counts what is done and names what is being written now", () => {
    const members = [
      member("1", { resolution_receipt: { status: "reconciled" } }),
      member("2"), member("3"), member("4"),
      { case_id: "aside", order_reference: "R5", set_aside: "period is locked" },
    ];
    const dispatch = { members: { "c-1": { status: "verified" }, "c-2": { status: "verified" }, "c-3": { status: "dispatching" }, "c-4": { status: "queued" } } };
    const p = groupProgress(members, dispatch);
    expect(p.total).toBe(4);
    expect(p.counts).toMatchObject({ reconciled: 1, rechecking: 1, writing: 1, queued: 1 });
    expect(p.done).toBe(2);
    expect(p.writing).toEqual(["R3"]);
  });
});
