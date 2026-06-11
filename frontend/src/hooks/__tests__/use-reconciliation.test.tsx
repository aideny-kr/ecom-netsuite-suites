import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";

const patch = vi.fn();
const post = vi.fn();
const get = vi.fn();
vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: (...a: unknown[]) => get(...a),
    post: (...a: unknown[]) => post(...a),
    patch: (...a: unknown[]) => patch(...a),
  },
}));

import { useApproveBucket, useApproveResult, useClosePeriod } from "@/hooks/use-reconciliation";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function makeWrapper(qc: QueryClient) {
  return function qcWrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

beforeEach(() => {
  patch.mockReset();
  post.mockReset();
  get.mockReset();
});

describe("useApproveResult", () => {
  it("invalidates recon-results AND recon-bucket-summary on success", async () => {
    // Regression: the CloseChecklist keys on ["recon-bucket-summary", runId]
    // (commit 5ae3b6d). A single-row approve from the results table must
    // refresh those counts too, or the checklist stays stale until refocus.
    patch.mockResolvedValue({ id: "res1", status: "approved" });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const { result } = renderHook(() => useApproveResult(), {
      wrapper: makeWrapper(qc),
    });
    result.current.mutate({ result_id: "res1", notes: "ok" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(patch).toHaveBeenCalledWith(
      "/api/v1/reconciliation/results/res1/approve",
      { result_id: "res1", notes: "ok" },
    );
    const invalidatedKeys = invalidateSpy.mock.calls.map(
      ([filters]) => filters?.queryKey,
    );
    expect(invalidatedKeys).toContainEqual(["recon-results"]);
    expect(invalidatedKeys).toContainEqual(["recon-bucket-summary"]);
  });
});

describe("useClosePeriod", () => {
  it("invalidates recon-runs, recon-results AND recon-bucket-summary on success", async () => {
    // Close locks rows server-side (status -> locked); only invalidating
    // recon-runs left the results table and the checklist's readiness counts
    // showing pre-close state until an unrelated refetch.
    post.mockResolvedValue({ period: "2026-05", runs_closed: 1 });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const { result } = renderHook(() => useClosePeriod(), {
      wrapper: makeWrapper(qc),
    });
    result.current.mutate("2026-05");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(post).toHaveBeenCalledWith("/api/v1/reconciliation/close/2026-05", {});
    const invalidatedKeys = invalidateSpy.mock.calls.map(
      ([filters]) => filters?.queryKey,
    );
    expect(invalidatedKeys).toContainEqual(["recon-runs"]);
    expect(invalidatedKeys).toContainEqual(["recon-results"]);
    expect(invalidatedKeys).toContainEqual(["recon-bucket-summary"]);
  });
});

describe("useApproveBucket", () => {
  it("POSTs to approve-bucket with run id, bucket and notes", async () => {
    post.mockResolvedValue({
      run_id: "r1",
      bucket: "matches",
      approved_count: 2,
      skipped_count: 0,
      correlation_id: "c1",
    });
    const { result } = renderHook(() => useApproveBucket("r1"), { wrapper });
    result.current.mutate({ bucket: "matches", notes: "close" });
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post).toHaveBeenCalledWith(
      "/api/v1/reconciliation/runs/r1/approve-bucket",
      { bucket: "matches", notes: "close" },
    );
  });
});
