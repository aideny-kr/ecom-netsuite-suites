import { describe, expect, it } from "vitest";
import { exactValue, parseRunScope, proposalState, runState } from "./format";

describe("transaction operations evidence presentation", () => {
  it("preserves exact monetary strings beyond JS precision and never guesses missing zero", () => {
    expect(exactValue("9999999999999999.0010")).toBe("9999999999999999.0010");
    expect(exactValue("-0.000")).toBe("-0.000");
    expect(exactValue(null)).toBe("Not provided");
    expect(exactValue(undefined)).toBe("Not provided");
    expect(exactValue({ tax: "0.010" })).toContain('"0.010"');
  });
  it("keeps full references, normalizes duplicates, rejects partial references and unbounded input", () => {
    expect(
      parseRunScope(
        "references",
        "R123456789-UK\nR123456789-EU,R123456789-UK",
        "",
        "",
      ),
    ).toEqual({ order_references: ["R123456789-EU", "R123456789-UK"] });
    expect(() => parseRunScope("references", "R123", "", "")).toThrow("full");
    expect(() => parseRunScope("references", "", "", "")).toThrow();
    expect(() =>
      parseRunScope(
        "references",
        Array.from(
          { length: 201 },
          (_, i) => `R${String(i).padStart(9, "0")}`,
        ).join("\n"),
        "",
        "",
      ),
    ).toThrow("200");
  });
  it("requires an increasing UTC window no longer than 31 days", () => {
    expect(
      parseRunScope("window", "", "2026-09-01T00:00", "2026-09-02T00:00"),
    ).toEqual({
      window_start: "2026-09-01T00:00:00.000Z",
      window_end: "2026-09-02T00:00:00.000Z",
    });
    expect(() => parseRunScope("window", "", "", "")).toThrow();
    expect(() =>
      parseRunScope("window", "", "2026-09-02T00:00", "2026-09-01T00:00"),
    ).toThrow();
    expect(() =>
      parseRunScope("window", "", "2026-01-01T00:00", "2026-03-01T00:00"),
    ).toThrow("31");
  });
  it("does not equate approval, expiry, unknown or failure with a successful write", () => {
    const future = "2026-09-04T16:00:00Z";
    const now = Date.parse("2026-09-04T15:00:00Z");
    expect(proposalState("approved", future, null, now)).toContain(
      "execution not verified",
    );
    expect(proposalState("approved", future, "unknown", now)).toContain(
      "unknown",
    );
    expect(proposalState("approved", future, "failed", now)).toContain(
      "failed",
    );
    expect(proposalState("approved", future, "verified", now)).toBe(
      "Execution verified",
    );
    expect(
      proposalState("pending", "2026-09-04T14:00:00Z", null, now),
    ).toContain("expired");
    expect(proposalState("pending", "invalid", null, now)).toContain("expired");
    expect(proposalState("rejected", future, null, now)).toBe("Rejected");
  });
  it("distinguishes budget or stalled termination from completion", () => {
    expect(runState("finished", "budget")).toBe("Stopped at budget");
    expect(runState("finished", "stall")).toBe("Stopped for review");
    expect(runState("finished", "done")).toBe("Investigation complete");
    expect(runState("running", null)).toBe("Investigating");
  });
});

it("rejects nonexistent calendar dates rather than silently investigating a different day", () => {
  expect(() =>
    parseRunScope("window", "", "2026-02-30T00:00", "2026-03-03T00:00"),
  ).toThrow("valid");
});
