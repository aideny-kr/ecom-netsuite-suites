import { describe, it, expect, vi } from "vitest";
import { consumeAgentLabStream } from "../agent-lab-stream";

/**
 * Helper: build a Response whose body is a ReadableStream that emits the given
 * chunks (strings) one at a time. Chunks are UTF-8 encoded.
 */
function responseFromChunks(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i]));
        i += 1;
      } else {
        controller.close();
      }
    },
  });
  return new Response(stream, {
    headers: { "Content-Type": "text/event-stream" },
  });
}

/**
 * Helper: build a Response that never closes its stream until the signal
 * aborts. Used to test abort behavior.
 */
function responseFromAbortableStream(signal: AbortSignal): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const onAbort = () => {
        try {
          controller.close();
        } catch {
          /* already closed */
        }
      };
      if (signal.aborted) onAbort();
      else signal.addEventListener("abort", onAbort, { once: true });
    },
  });
  return new Response(stream, {
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("consumeAgentLabStream — parser", () => {
  it("dispatches preparing → run_started → case_complete → run_complete in order", async () => {
    const body =
      `id: 1-0\nevent: preparing\ndata: {"phase":"loading"}\n\n` +
      `id: 2-0\nevent: run_started\ndata: {"total_cases":3,"estimated_cost_usd":1.5}\n\n` +
      `id: 3-0\nevent: case_complete\ndata: {"case_id":"c1","result":{"case_id":"c1","verdict":"TIE"},"cases_completed":1,"running_cost_usd":0.5}\n\n` +
      `id: 4-0\nevent: run_complete\ndata: {"status":"completed","total_cost_usd":1.5}\n\n`;

    const calls: string[] = [];
    const onPreparing = vi.fn(() => calls.push("preparing"));
    const onRunStarted = vi.fn(() => calls.push("run_started"));
    const onCaseComplete = vi.fn(() => calls.push("case_complete"));
    const onRunComplete = vi.fn(() => calls.push("run_complete"));

    await consumeAgentLabStream(responseFromChunks([body]), {
      onPreparing,
      onRunStarted,
      onCaseComplete,
      onRunComplete,
    });

    expect(calls).toEqual([
      "preparing",
      "run_started",
      "case_complete",
      "run_complete",
    ]);
    expect(onPreparing).toHaveBeenCalledWith({ phase: "loading" });
    expect(onRunStarted).toHaveBeenCalledWith({
      total_cases: 3,
      estimated_cost_usd: 1.5,
    });
    expect(onCaseComplete).toHaveBeenCalledWith({
      case_id: "c1",
      result: { case_id: "c1", verdict: "TIE" },
      cases_completed: 1,
      running_cost_usd: 0.5,
    });
    expect(onRunComplete).toHaveBeenCalledWith({
      status: "completed",
      total_cost_usd: 1.5,
    });
  });

  it("concatenates multi-line data: fields with \\n", async () => {
    // Split a single JSON object across two data: lines. Per SSE spec, the
    // receiver must concatenate them with \n before parsing.
    const multilineBody =
      `id: 1-0\nevent: preparing\ndata: {"phase":\ndata: "loading"}\n\n`;

    const onPreparing = vi.fn();
    await consumeAgentLabStream(responseFromChunks([multilineBody]), {
      onPreparing,
    });
    expect(onPreparing).toHaveBeenCalledWith({ phase: "loading" });
  });

  it("accumulates across chunk boundaries (\\n\\n split across reads)", async () => {
    // The \n\n separator between two events is split between two chunks.
    const chunk1 = `id: 1-0\nevent: preparing\ndata: {"phase":"loading"}\n`;
    const chunk2 =
      `\nid: 2-0\nevent: run_started\ndata: {"total_cases":2,"estimated_cost_usd":0.3}\n\n`;

    const onPreparing = vi.fn();
    const onRunStarted = vi.fn();
    await consumeAgentLabStream(responseFromChunks([chunk1, chunk2]), {
      onPreparing,
      onRunStarted,
    });

    expect(onPreparing).toHaveBeenCalledWith({ phase: "loading" });
    expect(onRunStarted).toHaveBeenCalledWith({
      total_cases: 2,
      estimated_cost_usd: 0.3,
    });
  });

  it("heartbeat (no id: line) fires onHeartbeat but does NOT update lastEventId", async () => {
    const body =
      `id: 1-0\nevent: preparing\ndata: {"phase":"loading"}\n\n` +
      `event: heartbeat\ndata: {"ts":1700000000}\n\n` +
      `id: 2-0\nevent: run_started\ndata: {"total_cases":1,"estimated_cost_usd":0.1}\n\n`;

    const onHeartbeat = vi.fn();
    const ids: string[] = [];
    const onEventId = vi.fn((id: string) => ids.push(id));

    await consumeAgentLabStream(responseFromChunks([body]), {
      onHeartbeat,
      onEventId,
    });

    expect(onHeartbeat).toHaveBeenCalledWith({ ts: 1700000000 });
    // heartbeat should NOT have pushed an id; only 1-0 and 2-0 come through
    expect(ids).toEqual(["1-0", "2-0"]);
  });

  it("tracks id: line via onEventId", async () => {
    const body =
      `id: 5-0\nevent: preparing\ndata: {"phase":"loading"}\n\n` +
      `id: 6-0\nevent: run_started\ndata: {"total_cases":1,"estimated_cost_usd":0.1}\n\n`;

    const ids: string[] = [];
    await consumeAgentLabStream(responseFromChunks([body]), {
      onEventId: (id) => ids.push(id),
    });

    expect(ids).toEqual(["5-0", "6-0"]);
  });

  it("stops reading after run_complete", async () => {
    const body =
      `id: 1-0\nevent: run_complete\ndata: {"status":"completed","total_cost_usd":0.1}\n\n` +
      `id: 2-0\nevent: preparing\ndata: {"phase":"loading"}\n\n`;

    const onRunComplete = vi.fn();
    const onPreparing = vi.fn();

    await consumeAgentLabStream(responseFromChunks([body]), {
      onRunComplete,
      onPreparing,
    });

    expect(onRunComplete).toHaveBeenCalledTimes(1);
    // Preparing event AFTER run_complete must NOT fire
    expect(onPreparing).not.toHaveBeenCalled();
  });

  it("error event fires onError and stops reading", async () => {
    const body =
      `id: 1-0\nevent: error\ndata: {"error":"boom"}\n\n` +
      `id: 2-0\nevent: preparing\ndata: {"phase":"loading"}\n\n`;

    const onError = vi.fn();
    const onPreparing = vi.fn();

    await consumeAgentLabStream(responseFromChunks([body]), {
      onError,
      onPreparing,
    });

    expect(onError).toHaveBeenCalledWith("boom");
    expect(onPreparing).not.toHaveBeenCalled();
  });

  it("abort signal exits cleanly without throwing", async () => {
    const controller = new AbortController();
    const response = responseFromAbortableStream(controller.signal);

    // Schedule the abort on the next tick so the parser is reading when it fires
    setTimeout(() => controller.abort(), 0);

    // Must not throw
    await expect(
      consumeAgentLabStream(response, {}, controller.signal),
    ).resolves.toBeUndefined();
  });
});
