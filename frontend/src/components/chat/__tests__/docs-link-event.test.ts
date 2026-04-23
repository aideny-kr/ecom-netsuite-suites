import { describe, it, expect } from "vitest";
import { normalizeStreamEvent } from "@/lib/chat-stream";

describe("normalizeStreamEvent(docs_link)", () => {
  it("parses a docs_link event into the typed shape", () => {
    const raw = {
      type: "docs_link",
      data: {
        url: "https://docs.google.com/document/d/FID",
        doc_id: "FID",
        title: "Q1 Research",
        shared_with: "user@example.com",
      },
    };
    const event = normalizeStreamEvent(raw);
    expect(event).not.toBeNull();
    expect(event?.type).toBe("docs_link");
    if (event?.type !== "docs_link") throw new Error("wrong type");
    expect(event.data.url).toBe("https://docs.google.com/document/d/FID");
    expect(event.data.doc_id).toBe("FID");
    expect(event.data.title).toBe("Q1 Research");
    expect(event.data.shared_with).toBe("user@example.com");
  });

  it("defaults title when missing", () => {
    const event = normalizeStreamEvent({
      type: "docs_link",
      data: { url: "https://x", doc_id: "FID" },
    });
    if (event?.type !== "docs_link") throw new Error("wrong type");
    expect(event.data.title).toBe("Document");
  });

  it("coerces shared_with to null when not a string", () => {
    const event = normalizeStreamEvent({
      type: "docs_link",
      data: { url: "https://x", doc_id: "FID", title: "t", shared_with: null },
    });
    if (event?.type !== "docs_link") throw new Error("wrong type");
    expect(event.data.shared_with).toBeNull();
  });

  it("returns null when data payload is missing", () => {
    expect(normalizeStreamEvent({ type: "docs_link" })).toBeNull();
  });
});
