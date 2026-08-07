import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import type { ReactNode } from "react";

const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  getText: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import {
  useComposePlaybook,
  useDeleteReport,
  usePinReport,
  useRefreshReport,
  useReport,
  useReportVersions,
  useResumeAutoRefresh,
  useUnpinReport,
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

it("useRefreshReport posts to /refresh and invalidates report + versions + dashboard queries", async () => {
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
  // Review fix: a report refreshed while it's the dashboard wallpaper must bust the
  // cached dashboard payload too — otherwise the wall shows new numbers (it refetches
  // the artifact by report id/version) under a stale "data as of" freshness chip
  // (the cached dashboard response) for up to staleTime.
  expect(keys).toContain(JSON.stringify(["dashboard"]));
});

// --- Task 4: delete -----------------------------------------------------------------

it("useDeleteReport DELETEs the report and invalidates the reports list + dashboard query", async () => {
  api.delete.mockResolvedValueOnce(undefined);
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useDeleteReport("r-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate();
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.delete).toHaveBeenCalledWith("/api/v1/reports/r-1");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["reports"]));
  // Review fix: deleting a report that's published/on someone's wall must bust the
  // cached dashboard payload — otherwise "Published dashboards" keeps listing it
  // (badged "On your wall"), its link 404s, Unpublish 404s silently, and the wall
  // renders "Preview unavailable" under the deleted title for up to staleTime.
  expect(keys).toContain(JSON.stringify(["dashboard"]));
});

// --- Task 5: pin/unpin --------------------------------------------------------------

it("usePinReport POSTs to /pin and invalidates report + list + dashboard queries", async () => {
  api.post.mockResolvedValueOnce({ id: "r-1", dashboard_pinned_at: "2026-07-22T10:00:00Z" });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => usePinReport("r-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate();
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/reports/r-1/pin");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["reports"]));
  expect(keys).toContain(JSON.stringify(["reports", "r-1"]));
  expect(keys).toContain(JSON.stringify(["dashboard"]));
});

it("useUnpinReport DELETEs /pin and invalidates report + list + dashboard queries", async () => {
  api.delete.mockResolvedValueOnce({ id: "r-1", dashboard_pinned_at: null });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useUnpinReport("r-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate();
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.delete).toHaveBeenCalledWith("/api/v1/reports/r-1/pin");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["reports"]));
  expect(keys).toContain(JSON.stringify(["reports", "r-1"]));
  expect(keys).toContain(JSON.stringify(["dashboard"]));
});

// --- Rolling-period Stage 1 (Task 5): compose with a tracking intent ---------------

it("useComposePlaybook defaults to mode: 'period' when the caller doesn't specify one (today's behaviour, unchanged)", async () => {
  api.post.mockResolvedValueOnce({ id: "r-1" });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useComposePlaybook(), { wrapper: makeWrapper(qc) });
  result.current.mutate({ key: "income_statement", params: { period: "Jun 2026" } });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/reports/playbooks/income_statement", {
    params: { period: "Jun 2026" },
    mode: "period",
  });
});

it("useComposePlaybook sends mode: 'tracking' when asked, with no params (the server resolves the period)", async () => {
  api.post.mockResolvedValueOnce({ id: "r-1", series_id: "s-1" });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useComposePlaybook(), { wrapper: makeWrapper(qc) });
  result.current.mutate({ key: "income_statement", params: {}, mode: "tracking" });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/reports/playbooks/income_statement", {
    params: {},
    mode: "tracking",
  });
});
