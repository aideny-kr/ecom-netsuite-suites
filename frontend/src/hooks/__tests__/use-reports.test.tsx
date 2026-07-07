import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  getText: vi.fn(),
}));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import {
  useRefreshReport,
  useReport,
  useReportVersions,
  useResumeAutoRefresh,
  useUpdateReportSettings,
} from "@/hooks/use-reports";

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

const qcOpts = { defaultOptions: { queries: { retry: false }, mutations: { retry: false } } };

it("useReport fetches the report metadata (incl. has_recipe/last_refreshed_at)", async () => {
  api.get.mockResolvedValueOnce({
    id: "r-1",
    title: "Live",
    status: "draft",
    version: 2,
    created_at: "2026-07-06T18:00:00Z",
    has_recipe: true,
    last_refreshed_at: "2026-07-07T03:00:00Z",
  });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useReport("r-1"), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.data?.has_recipe).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/reports/r-1");
  expect(result.current.data?.last_refreshed_at).toBe("2026-07-07T03:00:00Z");
});

it("useReportVersions fetches the picker entries", async () => {
  api.get.mockResolvedValueOnce([
    { version: 2, created_at: "2026-07-07T03:00:00Z", pinned: false, is_current: true },
    { version: 1, created_at: "2026-07-06T18:00:00Z", pinned: false, is_current: false },
  ]);
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useReportVersions("r-1"), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.data?.length).toBe(2));
  expect(api.get).toHaveBeenCalledWith("/api/v1/reports/r-1/versions");
});

// --- Slice C: auto-refresh settings + resume ------------------------------------------

it("useUpdateReportSettings PATCHes the interval and invalidates report queries", async () => {
  api.patch.mockResolvedValueOnce({ id: "r-1", auto_refresh: "hourly" });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useUpdateReportSettings("r-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate("hourly");
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.patch).toHaveBeenCalledWith("/api/v1/reports/r-1/settings", { auto_refresh: "hourly" });
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["reports"]));
  expect(keys).toContain(JSON.stringify(["reports", "r-1"]));
});

it("useResumeAutoRefresh posts to /auto-refresh/resume and invalidates report queries", async () => {
  api.post.mockResolvedValueOnce({ id: "r-1", auto_refresh_paused_at: null, refresh_failure_count: 0 });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useResumeAutoRefresh("r-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate();
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/reports/r-1/auto-refresh/resume");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["reports"]));
  expect(keys).toContain(JSON.stringify(["reports", "r-1"]));
});

it("useRefreshReport posts to /refresh and invalidates report + versions queries", async () => {
  api.post.mockResolvedValueOnce({ id: "r-1", version: 3, has_recipe: true });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useRefreshReport("r-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate();
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/reports/r-1/refresh");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["reports"]));
  expect(keys).toContain(JSON.stringify(["reports", "r-1"]));
  expect(keys).toContain(JSON.stringify(["reports", "r-1", "versions"]));
});
