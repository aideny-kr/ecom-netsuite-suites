import { describe, it, expect } from "vitest";
import { normalizeStreamMessage } from "@/lib/chat-stream";
import type { ClarificationData } from "@/lib/types";

describe("normalizeStreamMessage — clarification structured_output", () => {
  it("preserves type=clarification through normalization", () => {
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

    const raw = {
      id: "msg-1",
      role: "assistant",
      content: "",
      structured_output: clarification,
    };

    const msg = normalizeStreamMessage(raw);
    expect(msg).not.toBeNull();
    expect(msg?.structured_output).toBeDefined();
    expect(msg?.structured_output?.type).toBe("clarification");
    expect(msg?.structured_output).toEqual(clarification);
  });

  it("preserves chosen-state clarification", () => {
    const chosen: ClarificationData = {
      type: "clarification",
      status: "chosen",
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
          title: "BigQuery",
          rationale: "checkout",
          source: "bigquery",
          is_default: false,
        },
      ],
      default_id: "A",
      ambiguity_summary: "...",
      confirmation_token: "x".repeat(64),
      expires_at: "2099-01-01T00:00:00Z",
      chosen_id: "A",
      chose_at: "2026-04-28T18:00:01Z",
    };

    const raw = {
      id: "msg-2",
      role: "assistant",
      content: "",
      structured_output: chosen,
    };

    const msg = normalizeStreamMessage(raw);
    expect(msg?.structured_output?.type).toBe("clarification");
    const so = msg?.structured_output as unknown as ClarificationData;
    expect(so.chosen_id).toBe("A");
    expect(so.chose_at).toBe("2026-04-28T18:00:01Z");
  });

  it("returns null when content/role missing (consistent with other types)", () => {
    const raw = {
      structured_output: {
        type: "clarification",
        status: "pending",
      },
    };
    expect(normalizeStreamMessage(raw)).toBeNull();
  });
});
