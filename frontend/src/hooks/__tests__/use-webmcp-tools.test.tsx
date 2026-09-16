import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { useWebMcpTools } from "@/hooks/use-webmcp-tools";
import type { WebMcpTool } from "@/lib/webmcp";
import { apiClient } from "@/lib/api-client";

const auth = vi.hoisted(() => ({ user: { id: "user", tenant_id: "tenant", tenant_name: "Fixture" }, isLoading: false }));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => auth }));
vi.mock("@/lib/api-client", () => ({ apiClient: { get: vi.fn() }, ApiError: class extends Error {} }));

let registered: { tool: WebMcpTool; signal: AbortSignal }[];
beforeEach(() => {
  registered = [];
  vi.clearAllMocks();
  localStorage.setItem("access_token", "fixture-token");
  vi.mocked(apiClient.get).mockResolvedValue(auth.user);
  Object.defineProperty(document, "modelContext", { configurable: true, value: {
    registerTool: (tool: WebMcpTool, options: { signal: AbortSignal }) => registered.push({ tool, signal: options.signal }),
  } });
});

it("uses live handlers without re-registering and expires only its own tools", async () => {
  const execute = vi.fn(() => ({ value: 1 }));
  const nextExecute = vi.fn(() => ({ value: 2 }));
  const { rerender, unmount } = renderHook(({ handler, scope }) => useWebMcpTools(scope, [{
    name: "suitestudio_fixture", description: "Fixture", inputSchema: {},
    annotations: { readOnlyHint: true, untrustedContentHint: true }, execute: handler,
  }]), { initialProps: { handler: execute, scope: "first" } });
  await waitFor(() => expect(registered).toHaveLength(1));
  rerender({ handler: nextExecute, scope: "first" });
  expect(JSON.parse(await registered[0].tool.execute({}))).toEqual({ value: 2 });
  expect(execute).not.toHaveBeenCalled();
  rerender({ handler: nextExecute, scope: "second" });
  expect(registered[0].signal.aborted).toBe(true);
  expect(registered).toHaveLength(2);
  expect(JSON.parse(await registered[0].tool.execute({})).error).toBeDefined();
  unmount();
  expect(registered[1].signal.aborted).toBe(true);
});

it("discards late results after a token change and returns a retry hint", async () => {
  let resolve!: (value: unknown) => void;
  const { unmount } = renderHook(() => useWebMcpTools("fixture", [{
    name: "suitestudio_fixture", description: "Fixture", inputSchema: {},
    annotations: { readOnlyHint: true, untrustedContentHint: true },
    execute: () => new Promise((r) => { resolve = r; }),
  }]));
  const pending = registered[0].tool.execute({});
  await waitFor(() => expect(resolve).toBeDefined());
  act(() => { localStorage.setItem("access_token", "different-token"); resolve({ private: "old-tenant-result" }); });
  const result = await pending;
  expect(result).not.toContain("old-tenant-result");
  expect(JSON.parse(result).error.message).toContain("Session changed");
  unmount();
});
