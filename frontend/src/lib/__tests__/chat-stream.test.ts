import { describe, it, expect, vi } from "vitest";
import { consumeChatStream } from "../chat-stream";
import type { DisclosureBlock } from "../types";

function makeResponse(events: string[]): Response {
  const body = events.map((e) => `data: ${e}\n\n`).join("");
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

describe("consumeChatStream disclosure event", () => {
  it("parses a disclosure event and calls onDisclosure", async () => {
    const onDisclosure = vi.fn();
    const onMessage = vi.fn();
    const disclosure: DisclosureBlock = {
      source: "netsuite",
      interpretation: "This week (Monday–today)",
      implicit_filters: ["Excludes cancelled orders"],
      can_switch_source: true,
      is_rerun: false,
      failure_mode: false,
    };
    const events = [
      JSON.stringify({ type: "disclosure", ...disclosure }),
      JSON.stringify({
        type: "message",
        message: { id: "m1", role: "assistant", content: "hello" },
      }),
    ];
    await consumeChatStream(makeResponse(events), { onDisclosure, onMessage });
    expect(onDisclosure).toHaveBeenCalledWith(
      expect.objectContaining({
        source: "netsuite",
        interpretation: "This week (Monday–today)",
        can_switch_source: true,
      })
    );
  });

  it("disclosure is not a terminal event", async () => {
    const onDisclosure = vi.fn();
    const onMessage = vi.fn();
    const events = [
      JSON.stringify({
        type: "disclosure",
        source: "netsuite",
        interpretation: "test",
        implicit_filters: [],
        can_switch_source: false,
        is_rerun: false,
        failure_mode: false,
      }),
      JSON.stringify({
        type: "message",
        message: { id: "m1", role: "assistant", content: "done" },
      }),
    ];
    await consumeChatStream(makeResponse(events), { onDisclosure, onMessage });
    expect(onDisclosure).toHaveBeenCalled();
    expect(onMessage).toHaveBeenCalled();
  });
});
