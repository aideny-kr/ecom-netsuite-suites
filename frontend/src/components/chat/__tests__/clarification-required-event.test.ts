import { describe, it, expect } from "vitest";
import { normalizeStreamEvent, parseSseBuffer } from "@/lib/chat-stream";
import type { ClarificationData } from "@/lib/types";

describe("chat-stream parser — clarification_required SSE event", () => {
  // Codex P2 Bug 2a: the backend orchestrator emits
  //   {type: "clarification_required", data: ClarificationData}
  // mid-stream when the Plan Mode gate fires. The parser must recognize
  // this so the frontend can render the card immediately, not depend on
  // a refetch after the terminal `message` event.

  const clarification: ClarificationData = {
    type: "clarification",
    status: "pending",
    options: [
      {
        id: "A",
        title: "NetSuite GL",
        rationale: "recognized revenue",
        source: "netsuite",
        is_default: true,
      },
      {
        id: "B",
        title: "BigQuery checkout",
        rationale: "ecommerce totals",
        source: "bigquery",
        is_default: false,
      },
    ],
    default_id: "A",
    ambiguity_summary: "Revenue can mean two things.",
    confirmation_token: "deadbeef".repeat(8),
    expires_at: "2026-04-28T18:00:00Z",
  };

  it("normalizeStreamEvent recognizes clarification_required event", () => {
    const raw = {
      type: "clarification_required",
      data: clarification,
    };

    const event = normalizeStreamEvent(raw as unknown as Record<string, unknown>);
    expect(event).not.toBeNull();
    expect(event?.type).toBe("clarification_required");
    if (event?.type === "clarification_required") {
      expect(event.data).toEqual(clarification);
    }
  });

  it("parseSseBuffer extracts clarification_required from a stream chunk", () => {
    const sseChunk = `data: ${JSON.stringify({
      type: "clarification_required",
      data: clarification,
    })}\n\n`;
    const { events } = parseSseBuffer(sseChunk);
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("clarification_required");
    if (events[0].type === "clarification_required") {
      expect(events[0].data.options).toHaveLength(2);
      expect(events[0].data.default_id).toBe("A");
    }
  });

  it("returns null when clarification_required has no data payload", () => {
    const raw = {
      type: "clarification_required",
    };
    expect(normalizeStreamEvent(raw as unknown as Record<string, unknown>)).toBeNull();
  });
});
