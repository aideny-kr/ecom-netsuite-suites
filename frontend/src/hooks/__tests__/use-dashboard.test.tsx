import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import { useDashboard } from "@/hooks/use-dashboard";

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

const qcOpts = { defaultOptions: { queries: { retry: false }, mutations: { retry: false } } };

it("fetches GET /api/v1/dashboard under the ['dashboard'] query key", async () => {
  api.get.mockResolvedValueOnce({ published: [], active: null, active_is_fallback: false });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useDashboard(), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/dashboard");
  expect(qc.getQueryState(["dashboard"])).toBeDefined();
});

it("returns the published set and the active report", async () => {
  const report = {
    id: "r-1",
    title: "Income Statement — Jun 2026",
    status: "draft",
    version: 4,
    created_at: "2026-07-01T00:00:00Z",
    has_recipe: true,
  };
  api.get.mockResolvedValueOnce({ published: [report], active: report, active_is_fallback: false });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useDashboard(), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.data?.active?.id).toBe("r-1"));
  expect(result.current.data?.published).toHaveLength(1);
  expect(result.current.data?.active_is_fallback).toBe(false);
});
