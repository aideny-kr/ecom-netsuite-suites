import { describe, it, expect } from "vitest";
import { normalizeStreamEvent } from "@/lib/chat-stream";

describe("report_ready", () => {
  it("coerces a report_ready event", () => {
    const ev = normalizeStreamEvent({ type: "report_ready", data: { report_id: "abc", title: "Q2", url: "/reports/abc", section_count: 5 } });
    expect(ev).toEqual({ type: "report_ready", data: { report_id: "abc", title: "Q2", url: "/reports/abc", section_count: 5 } });
  });
});
