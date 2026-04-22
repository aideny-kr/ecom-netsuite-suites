import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useAgentLabRun } from "../use-agent-lab-run";
import { apiClient } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({
  apiClient: {
    streamGet: vi.fn(),
  },
}));

/**
 * Test helper: a controllable SSE source. Tests push one SSE event at a time
 * via `emit()`; the hook's parser reads them as they arrive. Call `close()` to
 * end the stream.
 */
interface SseController {
  emit(event: string, data: unknown, id?: string | null): void;
  close(): void;
  response: Response;
  signalRef: { current: AbortSignal | null };
}

function makeSseController(): SseController {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const response = new Response(stream, {
    headers: { "Content-Type": "text/event-stream" },
  });
  const signalRef: { current: AbortSignal | null } = { current: null };

  return {
    response,
    signalRef,
    emit(event, data, id = null) {
      const parts: string[] = [];
      if (id !== null) parts.push(`id: ${id}`);
      parts.push(`event: ${event}`);
      parts.push(`data: ${JSON.stringify(data)}`);
      controller.enqueue(encoder.encode(parts.join("\n") + "\n\n"));
    },
    close() {
      try {
        controller.close();
      } catch {
        /* already closed */
      }
    },
  };
}

describe("useAgentLabRun", () => {
  const mockStreamGet = vi.mocked(apiClient.streamGet);
  let sse: SseController;

  beforeEach(() => {
    sse = makeSseController();
    mockStreamGet.mockImplementation((_path: string, signal?: AbortSignal) => {
      sse.signalRef.current = signal ?? null;
      return Promise.resolve(sse.response);
    });
  });

  afterEach(() => {
    sse.close();
    vi.clearAllMocks();
  });

  it("calls apiClient.streamGet with run_id and last_id=0-0 on first mount", async () => {
    renderHook(() => useAgentLabRun("abc-123"));
    await waitFor(() => expect(mockStreamGet).toHaveBeenCalledTimes(1));
    const url = mockStreamGet.mock.calls[0][0];
    expect(url).toContain("/api/v1/agent-lab/runs/abc-123/events");
    // On first mount, last_id must be 0-0 (not empty) so backend sends all events
    expect(url).toContain("last_id=0-0");
  });

  it("transitions status: connecting → preparing → running → completed", async () => {
    const { result } = renderHook(() => useAgentLabRun("abc-123"));
    expect(result.current.status).toBe("connecting");
    await waitFor(() => expect(mockStreamGet).toHaveBeenCalled());

    sse.emit("preparing", { phase: "loading" }, "1-0");
    await waitFor(() => expect(result.current.status).toBe("preparing"));
    expect(result.current.preparingPhase).toBe("loading");

    sse.emit("run_started", { total_cases: 18, estimated_cost_usd: 6.3 }, "2-0");
    await waitFor(() => expect(result.current.status).toBe("running"));
    expect(result.current.totalCases).toBe(18);

    sse.emit(
      "run_complete",
      { status: "completed", total_cost_usd: 5.84 },
      "3-0",
    );
    await waitFor(() => expect(result.current.status).toBe("completed"));
  });

  it("accumulates case_complete events", async () => {
    const { result } = renderHook(() => useAgentLabRun("abc-123"));
    await waitFor(() => expect(mockStreamGet).toHaveBeenCalled());

    sse.emit("run_started", { total_cases: 18, estimated_cost_usd: 6.3 }, "1-0");
    await waitFor(() => expect(result.current.totalCases).toBe(18));

    sse.emit(
      "case_complete",
      {
        case_id: "c1",
        cases_completed: 1,
        running_cost_usd: 0.35,
        result: { case_id: "c1", verdict: "TIE" },
      },
      "2-0",
    );
    await waitFor(() => expect(result.current.casesCompleted).toBe(1));
    expect(result.current.runningCost).toBeCloseTo(0.35);
    expect(result.current.cases).toHaveLength(1);
    expect(result.current.cases[0].case_id).toBe("c1");
  });

  it("does not duplicate cases when the same case_id is received twice", async () => {
    const { result } = renderHook(() => useAgentLabRun("abc-123"));
    await waitFor(() => expect(mockStreamGet).toHaveBeenCalled());

    sse.emit("run_started", { total_cases: 18, estimated_cost_usd: 6.3 }, "1-0");
    await waitFor(() => expect(result.current.totalCases).toBe(18));

    const payload = {
      case_id: "c1",
      cases_completed: 1,
      running_cost_usd: 0.35,
      result: { case_id: "c1", verdict: "TIE" },
    };
    // Same case_id emitted twice (simulates reconnect replay)
    sse.emit("case_complete", payload, "2-0");
    sse.emit("case_complete", payload, "3-0");

    await waitFor(() => expect(result.current.casesCompleted).toBe(1));
    expect(result.current.cases).toHaveLength(1);
  });

  it("resumes from last seen id on remount with same runId", async () => {
    const { rerender } = renderHook(({ id }) => useAgentLabRun(id), {
      initialProps: { id: "abc-123" as string | null },
    });
    await waitFor(() => expect(mockStreamGet).toHaveBeenCalledTimes(1));
    expect(mockStreamGet.mock.calls[0][0]).toContain("last_id=0-0");

    // Emit some events with ids — the hook should remember the last one
    sse.emit("preparing", { phase: "loading" }, "5-0");
    sse.emit("run_started", { total_cases: 1, estimated_cost_usd: 0.1 }, "7-0");
    await waitFor(() => expect(mockStreamGet).toHaveBeenCalledTimes(1));

    // Toggle the hook off and back on to force a new effect with the same runId
    // (simulates navigate-away-and-back).
    const firstSse = sse;
    sse = makeSseController();
    mockStreamGet.mockImplementation(
      (_path: string, signal?: AbortSignal) => {
        sse.signalRef.current = signal ?? null;
        return Promise.resolve(sse.response);
      },
    );
    rerender({ id: null });
    firstSse.close();
    rerender({ id: "abc-123" });

    await waitFor(() => expect(mockStreamGet).toHaveBeenCalledTimes(2));
    const secondUrl = mockStreamGet.mock.calls[1][0];
    // The hook must send the LAST seen id (7-0), encoded
    expect(secondUrl).toContain(`last_id=${encodeURIComponent("7-0")}`);
  });

  it("sets error + status=failed on error event", async () => {
    const { result } = renderHook(() => useAgentLabRun("abc-123"));
    await waitFor(() => expect(mockStreamGet).toHaveBeenCalled());

    sse.emit("error", { error: "backend boom" }, "1-0");
    await waitFor(() => expect(result.current.error).toBe("backend boom"));
    expect(result.current.status).toBe("failed");
  });

  it("aborts the fetch on unmount", async () => {
    const { unmount } = renderHook(() => useAgentLabRun("abc-123"));
    await waitFor(() => expect(sse.signalRef.current).not.toBeNull());
    expect(sse.signalRef.current?.aborted).toBe(false);
    unmount();
    expect(sse.signalRef.current?.aborted).toBe(true);
  });
});
