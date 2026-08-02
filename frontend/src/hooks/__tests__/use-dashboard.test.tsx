import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), delete: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import {
  useClearActiveDashboard,
  useDashboard,
  useDismissDashboardNotice,
  useSetActiveDashboard,
} from "@/hooks/use-dashboard";

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

// --- Task 4: switcher mutations ------------------------------------------------

it("useSetActiveDashboard PUTs the chosen report id and invalidates ['dashboard']", async () => {
  const response = { published: [], active: null, active_is_fallback: false };
  api.put.mockResolvedValueOnce(response);
  const qc = new QueryClient(qcOpts);
  const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useSetActiveDashboard(), { wrapper: makeWrapper(qc) });

  await act(async () => {
    await result.current.mutateAsync("r-9");
  });

  expect(api.put).toHaveBeenCalledWith("/api/v1/dashboard/active", { report_id: "r-9" });
  expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["dashboard"] });
});

it("useClearActiveDashboard DELETEs the selection and invalidates ['dashboard']", async () => {
  const response = { published: [], active: null, active_is_fallback: false };
  api.delete.mockResolvedValueOnce(response);
  const qc = new QueryClient(qcOpts);
  const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useClearActiveDashboard(), { wrapper: makeWrapper(qc) });

  await act(async () => {
    await result.current.mutateAsync();
  });

  expect(api.delete).toHaveBeenCalledWith("/api/v1/dashboard/active");
  expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["dashboard"] });
});

// --- Round-3 T2-gate fix: explicit, persisted notice dismissal -----------------
// GET is pure now (never consumes the tombstone as a read side effect), so the
// dismissible fallback banner in DashboardWall must call this mutation for the
// dismissal to actually persist server-side.

it("useDismissDashboardNotice POSTs to /dashboard/notice/dismiss and invalidates ['dashboard']", async () => {
  const response = { published: [], active: null, active_is_fallback: false };
  api.post.mockResolvedValueOnce(response);
  const qc = new QueryClient(qcOpts);
  const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useDismissDashboardNotice(), { wrapper: makeWrapper(qc) });

  await act(async () => {
    await result.current.mutateAsync();
  });

  expect(api.post).toHaveBeenCalledWith("/api/v1/dashboard/notice/dismiss");
  expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["dashboard"] });
});
