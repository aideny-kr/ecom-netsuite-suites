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

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
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

    const { result } = renderHook(() => useWorkspaceChat(workspaceId), { wrapper });

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

    const { result } = renderHook(() => useWorkspaceChat(workspaceId), { wrapper });

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
});
