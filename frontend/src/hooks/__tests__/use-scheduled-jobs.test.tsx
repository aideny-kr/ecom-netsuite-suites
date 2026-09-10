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
  useApproveSchedule,
  useCreateSchedule,
  useDeleteSchedule,
  usePauseSchedule,
  useResumeScheduledJob,
  useRunSchedule,
  useRunScheduleNow,
  useScheduledJob,
  useScheduledJobs,
  useScheduleRuns,
  useUpdateSchedule,
} from "@/hooks/use-scheduled-jobs";

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

const qcOpts = { defaultOptions: { queries: { retry: false }, mutations: { retry: false } } };

it("useScheduledJobs GETs /api/v1/schedules", async () => {
  // Task 5 residual: the response is {schedules, runs_last_7_days_total,
  // runs_last_7_days_failed} — a wrapper, not a bare array — since the
  // list page's "Last 7 days" tile is a tenant-wide aggregate the rows
  // alone can't carry.
  api.get.mockResolvedValueOnce({
    schedules: [{ id: "s-1", name: "Inventory Aging Weekly" }],
    runs_last_7_days_total: 21,
    runs_last_7_days_failed: 1,
  });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useScheduledJobs(), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/schedules");
  expect(result.current.data?.schedules[0].name).toBe("Inventory Aging Weekly");
  expect(result.current.data?.runs_last_7_days_total).toBe(21);
  expect(result.current.data?.runs_last_7_days_failed).toBe(1);
});

it("useRunScheduleNow POSTs /run with use_pending: false and invalidates the list", async () => {
  api.post.mockResolvedValueOnce({ jobs_id: "j-1", reason: "done", outputs: {} });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useRunScheduleNow(), { wrapper: makeWrapper(qc) });
  result.current.mutate("s-1");
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/schedules/s-1/run", { use_pending: false });
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["scheduled-jobs"]));
});

it("useResumeScheduledJob POSTs /resume and invalidates the list", async () => {
  api.post.mockResolvedValueOnce({ id: "s-3", paused_at: null });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useResumeScheduledJob(), { wrapper: makeWrapper(qc) });
  result.current.mutate("s-3");
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/schedules/s-3/resume");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["scheduled-jobs"]));
});

// --- Task 6 (job detail page) additions ------------------------------------

it("useScheduledJob GETs /api/v1/schedules/{id} keyed by id", async () => {
  api.get.mockResolvedValueOnce({ id: "s-1", name: "Inventory Aging Weekly", plan_json: null });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useScheduledJob("s-1"), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/schedules/s-1");
  expect(result.current.data?.name).toBe("Inventory Aging Weekly");
});

it("useScheduleRuns GETs /api/v1/schedules/{id}/runs", async () => {
  api.get.mockResolvedValueOnce([{ id: "j-1", status: "completed", reason: "done", outputs: {} }]);
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useScheduleRuns("s-1"), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/schedules/s-1/runs");
  expect(result.current.data?.[0].id).toBe("j-1");
});

it("useUpdateSchedule PATCHes /api/v1/schedules/{id} with the given body and invalidates scheduled-jobs", async () => {
  api.patch.mockResolvedValueOnce({ id: "s-1" });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useUpdateSchedule("s-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate({ instruction: "and Virtual" });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.patch).toHaveBeenCalledWith("/api/v1/schedules/s-1", { instruction: "and Virtual" });
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["scheduled-jobs"]));
});

it("useApproveSchedule POSTs /approve and invalidates scheduled-jobs", async () => {
  api.post.mockResolvedValueOnce({ id: "s-1", plan_status: "approved" });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useApproveSchedule("s-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate();
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/schedules/s-1/approve");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["scheduled-jobs"]));
});

it("useRunSchedule POSTs /run with the given use_pending flag", async () => {
  api.post.mockResolvedValueOnce({ jobs_id: "j-2", reason: "done", outputs: {} });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useRunSchedule("s-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate(true);
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/schedules/s-1/run", { use_pending: true });
});

it("usePauseSchedule POSTs /pause and invalidates scheduled-jobs", async () => {
  api.post.mockResolvedValueOnce({ id: "s-1", paused_at: "2026-09-08T00:00:00Z" });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => usePauseSchedule("s-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate();
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/schedules/s-1/pause");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["scheduled-jobs"]));
});

it("useDeleteSchedule DELETEs /api/v1/schedules/{id} and invalidates scheduled-jobs", async () => {
  api.delete.mockResolvedValueOnce(undefined);
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useDeleteSchedule("s-1"), { wrapper: makeWrapper(qc) });
  result.current.mutate();
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.delete).toHaveBeenCalledWith("/api/v1/schedules/s-1");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["scheduled-jobs"]));
});

// ---------------------------------------------------------------------------
// New job flow (Task 7) — the compile-then-approve create path.
// ---------------------------------------------------------------------------

it("useCreateSchedule POSTs /api/v1/schedules with the instruction and invalidates scheduled-jobs", async () => {
  api.post.mockResolvedValueOnce({ id: "s-9", plan_status: "pending_approval" });
  const qc = new QueryClient(qcOpts);
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useCreateSchedule(), { wrapper: makeWrapper(qc) });
  result.current.mutate({ instruction: "every Friday email the exception summary" });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.post).toHaveBeenCalledWith("/api/v1/schedules", {
    instruction: "every Friday email the exception summary",
  });
  expect(result.current.data?.id).toBe("s-9");
  const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
  expect(keys).toContain(JSON.stringify(["scheduled-jobs"]));
});
