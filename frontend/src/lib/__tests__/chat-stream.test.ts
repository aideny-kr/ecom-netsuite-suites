import { describe, it, expect, vi } from "vitest";
import { parseSseBuffer, consumeChatStream } from "../chat-stream";
import type { DisclosureBlock } from "@/lib/types";

describe("parseSseBuffer — disclosure event", () => {
  it("parses a disclosure SSE payload into DisclosureBlock", () => {
    const buf =
      'data: {"type":"disclosure","source":"netsuite","interpretation":"\\"This week\\" = current week","implicit_filters":["Excludes cancelled records"],"can_switch_source":true,"is_rerun":false,"failure_mode":false}\n\n';
    const { events, remainder } = parseSseBuffer(buf);
    expect(remainder).toBe("");
    expect(events).toHaveLength(1);
    const e = events[0];
    expect(e.type).toBe("disclosure");
    if (e.type === "disclosure") {
      expect(e.disclosure.source).toBe("netsuite");
      expect(e.disclosure.interpretation).toContain("week");
      expect(e.disclosure.implicit_filters).toEqual(["Excludes cancelled records"]);
      expect(e.disclosure.can_switch_source).toBe(true);
      expect(e.disclosure.is_rerun).toBe(false);
      expect(e.disclosure.failure_mode).toBe(false);
    }
  });

  it("falls back to netsuite for unknown source values", () => {
    const buf =
      'data: {"type":"disclosure","source":"unknown","interpretation":"","implicit_filters":[],"can_switch_source":false,"is_rerun":false,"failure_mode":false}\n\n';
    const { events } = parseSseBuffer(buf);
    expect(events[0].type).toBe("disclosure");
    if (events[0].type === "disclosure") {
      expect(events[0].disclosure.source).toBe("netsuite");
    }
  });
});

describe("consumeChatStream — disclosure event dispatch", () => {
  function makeResponse(chunks: string[]): Response {
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        for (const c of chunks) controller.enqueue(encoder.encode(c));
        controller.close();
      },
    });
    return new Response(stream);
  }

  it("calls onDisclosure and continues until message arrives", async () => {
    const onDisclosure = vi.fn();
    const onMessage = vi.fn();
    const res = makeResponse([
      'data: {"type":"disclosure","source":"bigquery","interpretation":"","implicit_filters":[],"can_switch_source":true,"is_rerun":false,"failure_mode":false}\n\n',
      'data: {"type":"message","message":{"id":"m1","role":"assistant","content":"done","created_at":"2026-04-08T00:00:00Z"}}\n\n',
    ]);
    await consumeChatStream(res, { onDisclosure, onMessage });
    expect(onDisclosure).toHaveBeenCalledTimes(1);
    const d = onDisclosure.mock.calls[0][0] as DisclosureBlock;
    expect(d.source).toBe("bigquery");
    expect(onMessage).toHaveBeenCalledTimes(1);
  });

  it("disclosure event alone does NOT terminate the stream", async () => {
    const onDisclosure = vi.fn();
    const onMessage = vi.fn();
    const res = makeResponse([
      'data: {"type":"disclosure","source":"netsuite","interpretation":"","implicit_filters":[],"can_switch_source":false,"is_rerun":false,"failure_mode":false}\n\n',
      // No message event — stream closes naturally
    ]);
    await consumeChatStream(res, { onDisclosure, onMessage });
    expect(onDisclosure).toHaveBeenCalledTimes(1);
    expect(onMessage).not.toHaveBeenCalled();
  });
});
