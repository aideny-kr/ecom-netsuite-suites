import { describe, expect, it } from "vitest";
import { accessState, systemKey } from "./connection-health";

describe("connection diagnostics", () => {
  it("does not let an active transport mask partial or expired access", () => {
    expect(accessState({ status: "active", verification_status: "partial" })).toBe("Partially verified");
    expect(accessState({ status: "needs_reauth", verification_status: "ok" })).toBe("Authorization expired");
    expect(accessState({ status: "refresh_required" })).toBe("Token refresh needed");
    expect(accessState({ status: "disabled" })).toBe("Agent access disabled");
  });
  it("does not call unchecked access healthy", () => {
    expect(accessState({ status: "active" })).toBe("Not yet verified");
    expect(accessState({ status: "active", last_health_check: "2026-09-01" })).toBe("Active at last check");
    expect(accessState({ status: "active", verification_status: "ok" })).toBe("Verified at last test");
  });
  it("groups API and MCP methods without conflating unrelated custom sources", () => {
    expect(systemKey("netsuite")).toBe(systemKey("netsuite_mcp"));
    expect(systemKey("custom", "https://one.test/mcp")).not.toBe(systemKey("custom", "https://two.test/mcp"));
    expect(systemKey("custom", "https://bi.test/api/metabase-mcp")).toBe("metabase:bi.test");
  });
});
