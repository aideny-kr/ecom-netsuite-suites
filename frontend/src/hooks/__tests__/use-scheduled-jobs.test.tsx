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

import { useResumeScheduledJob, useRunScheduleNow, useScheduledJobs } from "@/hooks/use-scheduled-jobs";

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

const qcOpts = { defaultOptions: { queries: { retry: false }, mutations: { retry: false } } };

it("useScheduledJobs GETs /api/v1/schedules", async () => {
  api.get.mockResolvedValueOnce([{ id: "s-1", name: "Inventory Aging Weekly" }]);
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useScheduledJobs(), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/schedules");
  expect(result.current.data?.[0].name).toBe("Inventory Aging Weekly");
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
