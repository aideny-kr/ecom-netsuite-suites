/**
 * Behavioral tests for useWorkspaceChat.
 *
 * Staging bug 2026-05-18: workspace chat hangs in the UI even though the
 * assistant response is persisted to the DB. The chat appears "dead" because
 * the hook POSTs to /messages and tries to read the POST response body as an
 * SSE stream — but /messages returns plain JSON `{run_id, session_id}` since
 * chat moved to background tasks (PR #23). The real SSE stream lives at
 * `/api/v1/chat/runs/{run_id}/stream?last_id=0` (same pattern as the regular
 * chat page).
 *
 * These tests pin the contract: handleSend must POST to /messages, extract
 * run_id, then connect to the SSE GET endpoint. A `useEffect` reconnects to
 * an in-flight run when the user navigates back to a session with status=
 * "running".
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";

import { useWorkspaceChat } from "@/hooks/use-workspace-chat";

vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    stream: vi.fn(),
    streamGet: vi.fn(),
  },
}));

import { apiClient } from "@/lib/api-client";

function makeSseResponse(events: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const ev of events) {
        controller.enqueue(encoder.encode(ev));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function Wrapper({ children }: { children: React.ReactNode }) {
  const [qc] = React.useState(() => new QueryClient({
    defaultOptions: { queries: { retry: false } },
  }));
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useWorkspaceChat — SSE stream contract", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default empty session list
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("handleSend POSTs /messages then connects to /runs/{run_id}/stream via streamGet", async () => {
    const workspaceId = "ws-1";
    const newSession = { id: "sess-1", workspace_id: workspaceId };
    const runId = "run-abc";

    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
      if (path === "/api/v1/chat/sessions") return Promise.resolve(newSession);
      if (path.endsWith("/messages"))
        return Promise.resolve({ run_id: runId, session_id: newSession.id });
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });

    const sseEvents = [
      `data: ${JSON.stringify({ type: "text", content: "Hello" })}\n\n`,
      `data: ${JSON.stringify({
        type: "message",
        message: {
          id: "m1",
          role: "assistant",
          content: "Hello world",
          created_at: "2026-05-18T22:00:00Z",
        },
      })}\n\n`,
    ];
    (apiClient.streamGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSseResponse(sseEvents),
    );

    const { result } = renderHook(() => useWorkspaceChat(workspaceId), { wrapper: Wrapper });

    await act(async () => {
      await result.current.handleSend("explain me about this script");
    });

    // Wait for the stream to be consumed and state to settle
    await waitFor(() => {
      expect(apiClient.streamGet).toHaveBeenCalled();
    });

    // POST went to the messages endpoint
    const postCalls = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls;
    expect(
      postCalls.some(([path]) => typeof path === "string" && path.endsWith("/messages")),
    ).toBe(true);

    // SSE stream connected via streamGet to /runs/{run_id}/stream
    const streamGetCalls = (apiClient.streamGet as ReturnType<typeof vi.fn>).mock.calls;
    expect(streamGetCalls.length).toBeGreaterThan(0);
    const [streamPath] = streamGetCalls[0];
    expect(streamPath).toContain(`/api/v1/chat/runs/${runId}/stream`);
    expect(streamPath).toContain("last_id=0");
  });

  it("keeps isSending true continuously from handleSend through stream connect", async () => {
    // Bug: workspace chat looked dead because isSending dropped to false in
    // the window between createSession resolving and connectToRunStream
    // actually setting setIsStreaming(true). With no indicator the user
    // assumed the chat stopped working.
    const workspaceId = "ws-1";
    const newSession = { id: "sess-busy", workspace_id: workspaceId };
    const runId = "run-busy";

    // Defer createSession + POST so we can sample isSending mid-flight
    let resolveCreate: (v: typeof newSession) => void = () => {};
    let resolvePost: (v: { run_id: string; session_id: string }) => void = () => {};
    const createPromise = new Promise<typeof newSession>((r) => {
      resolveCreate = r;
    });
    const postPromise = new Promise<{ run_id: string; session_id: string }>((r) => {
      resolvePost = r;
    });
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
      if (path === "/api/v1/chat/sessions") return createPromise;
      if (path.endsWith("/messages")) return postPromise;
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    (apiClient.streamGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSseResponse([]),
    );

    const { result } = renderHook(() => useWorkspaceChat(workspaceId), { wrapper: Wrapper });

    expect(result.current.isSending).toBe(false);

    // Kick off send; do NOT await yet
    let sendPromise: ReturnType<typeof result.current.handleSend> | undefined;
    act(() => {
      sendPromise = result.current.handleSend("hello");
    });

    // Immediately busy (createSession.isPending)
    await waitFor(() => expect(result.current.isSending).toBe(true));

    // The critical assertion: resolving createSession used to flip
    // createSession.isPending to false, and because handleSend never set
    // isStreaming up front, isSending briefly became false here. Tests
    // post-fix: isSending must stay true through this transition so the
    // typing indicator never blinks out.
    await act(async () => {
      resolveCreate(newSession);
      await Promise.resolve();
      await Promise.resolve(); // two ticks: createSession resolve → handleSend resumes
    });
    expect(result.current.isSending).toBe(true);

    // Drain the rest cleanly so the test doesn't leave a pending promise.
    await act(async () => {
      resolvePost({ run_id: runId, session_id: newSession.id });
      await sendPromise;
    });
    await waitFor(() => expect(result.current.isSending).toBe(false));
  });

  it("reconnects to an active run when sessionDetail reports status=running", async () => {
    const workspaceId = "ws-1";
    const sessionId = "sess-2";
    const runId = "run-resume";

    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
      if (path.startsWith("/api/v1/chat/sessions?workspace_id=")) {
        return Promise.resolve([{ id: sessionId, workspace_id: workspaceId }]);
      }
      if (path === `/api/v1/chat/sessions/${sessionId}`) {
        return Promise.resolve({
          id: sessionId,
          workspace_id: workspaceId,
          messages: [],
          active_run_id: runId,
          status: "running",
        });
      }
      return Promise.resolve([]);
    });

    (apiClient.streamGet as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSseResponse([
        `data: ${JSON.stringify({ type: "text", content: "Resumed" })}\n\n`,
      ]),
    );

    const { result } = renderHook(() => useWorkspaceChat(workspaceId), { wrapper: Wrapper });

    act(() => {
      result.current.setActiveSessionId(sessionId);
    });

    await waitFor(() => {
      const calls = (apiClient.streamGet as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls.length).toBeGreaterThan(0);
      const [path] = calls[0];
      expect(path).toContain(`/api/v1/chat/runs/${runId}/stream`);
      expect(path).toContain("last_id=0");
    });
  });
  it("returns keyed admission before streaming finishes, preserves attachment retries and cancels only the active run", async () => {
    const receipt = { session_id: "s1", run_id: "r1", request_id: "key1" };
    vi.mocked(apiClient.post).mockResolvedValue(receipt);
    vi.mocked(apiClient.streamGet).mockImplementation(() => new Promise(() => {}));
    const { result } = renderHook(() => useWorkspaceChat("w1"), { wrapper: Wrapper });
    act(() => result.current.setActiveSessionId("s1"));
    await act(async () => {
      expect(await result.current.handleSend("long input", "attachment1", { request_id: "key1" })).toEqual(receipt);
    });
    expect(result.current.isSending).toBe(true);
    await act(async () => {
      await result.current.handleSend("long input", "attachment1", { request_id: "key1" });
    });
    expect(vi.mocked(apiClient.post).mock.calls.slice(0, 2)).toEqual([
      ["/api/v1/chat/sessions/s1/messages", { content: "long input", file_id: "attachment1", request_id: "key1" }],
      ["/api/v1/chat/sessions/s1/messages", { content: "long input", file_id: "attachment1", request_id: "key1" }],
    ]);
    expect(apiClient.streamGet).toHaveBeenCalledTimes(1);
    await expect(result.current.cancelActiveRun("wrong")).rejects.toThrow("matching active");
    await act(async () => { await result.current.handleStop(); });
    expect(apiClient.post).toHaveBeenLastCalledWith("/api/v1/chat/runs/r1/cancel", {});
  });

  it("does not dispatch a message after changing workspace during session creation", async () => {
    let resolveCreate!: (session: unknown) => void;
    vi.mocked(apiClient.post).mockImplementation(() => new Promise((resolve) => { resolveCreate = resolve; }));
    const { result, rerender } = renderHook(({ id }) => useWorkspaceChat(id), { initialProps: { id: "w1" }, wrapper: Wrapper });
    let pending: ReturnType<typeof result.current.handleSend>;
    act(() => { pending = result.current.handleSend("original workspace only"); });
    await waitFor(() => expect(resolveCreate).toBeDefined());
    rerender({ id: "w2" });
    await act(async () => { resolveCreate({ id: "s1", workspace_id: "w1" }); await pending; });
    expect(apiClient.post).toHaveBeenCalledTimes(1);
    expect(result.current.activeSessionId).toBe(null);
    await waitFor(() => expect(result.current.isSending).toBe(false));
    expect(apiClient.streamGet).not.toHaveBeenCalled();
  });

  it("ignores an old stream's late events and cleanup after selecting a new conversation", async () => {
    let oldController!: ReadableStreamDefaultController<Uint8Array>;
    const oldStream = new Response(new ReadableStream<Uint8Array>({ start(controller) { oldController = controller; } }));
    vi.mocked(apiClient.streamGet).mockResolvedValueOnce(oldStream).mockImplementation(() => new Promise(() => {}));
    vi.mocked(apiClient.post).mockImplementation(async (path) => ({ session_id: path.includes("s1") ? "s1" : "s2", run_id: path.includes("s1") ? "r1" : "r2" }));
    const { result } = renderHook(() => useWorkspaceChat("w1"), { wrapper: Wrapper });
    act(() => result.current.setActiveSessionId("s1"));
    await act(async () => { await result.current.handleSend("first", undefined, { request_id: "k1" }); });
    act(() => result.current.setActiveSessionId("s2"));
    await act(async () => { await result.current.handleSend("second", undefined, { request_id: "k2" }); });
    await act(async () => {
      oldController.enqueue(new TextEncoder().encode('data: {"type":"error","message":"stale error"}\n\ndata: {"type":"text","content":"stale text"}\n\n'));
      oldController.close();
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(result.current.activeSessionId).toBe("s2");
    expect(result.current.activeRunId).toBe("r2");
    expect(result.current.isSending).toBe(true);
    expect(result.current.error).toBe(null);
    expect(result.current.streamBlocks).toEqual([]);
  });

});
