import { describe, it, expect, vi } from "vitest";
import { consumeChatStream, normalizeStreamEvent, parseSseBuffer } from "@/lib/chat-stream";
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

  // Codex round 10 P2 Bug 2: the parser recognizes the event but the
  // dispatch loop in `consumeChatStream` had no branch for it — the
  // `onClarificationRequired` handler is never called. The card only
  // appears at the end of the turn (via the terminal `message`'s
  // `structured_output`), defeating the whole point of the mid-stream
  // gate event.
  it("consumeChatStream dispatches clarification_required to onClarificationRequired", async () => {
    const sseStream = [
      `data: ${JSON.stringify({ type: "clarification_required", data: clarification })}\n\n`,
      `data: ${JSON.stringify({
        type: "message",
        message: {
          id: "msg-1",
          role: "assistant",
          content: "",
          created_at: "2026-04-28T18:00:00Z",
          structured_output: clarification,
        },
      })}\n\n`,
    ].join("");

    // Build a minimal Response whose body streams the SSE bytes.
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(sseStream));
        controller.close();
      },
    });
    const res = new Response(body, {
      headers: { "Content-Type": "text/event-stream" },
    });

    const onClarificationRequired = vi.fn();
    const onMessage = vi.fn();

    await consumeChatStream(res, {
      onClarificationRequired,
      onMessage,
    });

    expect(onClarificationRequired).toHaveBeenCalledTimes(1);
    expect(onClarificationRequired).toHaveBeenCalledWith(clarification);
    // The terminal `message` should still be dispatched — clarification_required
    // is mid-stream, not terminal.
    expect(onMessage).toHaveBeenCalledTimes(1);
  });
});
