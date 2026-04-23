import { describe, it, expect } from "vitest";
import { normalizeStreamMessage } from "@/lib/chat-stream";

describe("normalizeStreamMessage preserves drive_sources", () => {
  it("passes through a drive_sources structured_output payload", () => {
    const raw = {
      id: "m1",
      role: "assistant",
      content: "Policy ref [Returns Policy].",
      structured_output: {
        type: "drive_sources",
        data: { "Returns Policy": "https://docs.google.com/document/d/xyz/edit" },
      },
      created_at: "2026-04-22T00:00:00Z",
    };
    const msg = normalizeStreamMessage(raw);
    expect(msg).not.toBeNull();
    expect(msg?.structured_output).toBeDefined();
    expect(msg?.structured_output?.type).toBe("drive_sources");
    expect((msg?.structured_output?.data as Record<string, string>)["Returns Policy"]).toBe(
      "https://docs.google.com/document/d/xyz/edit",
    );
  });

  it("returns null when content is missing", () => {
    expect(normalizeStreamMessage({ role: "assistant" })).toBeNull();
  });
});
