import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";

const stream = vi.fn();
vi.mock("@/lib/api-client", () => ({
  apiClient: {
    stream: (...a: unknown[]) => stream(...a),
  },
}));

import { useReconPipeline } from "@/hooks/use-recon-pipeline";

function makeWrapper(qc: QueryClient) {
  return function qcWrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

/** Minimal SSE response double: one encoded chunk of `data: {...}\n\n` events,
 *  then done — matches what apiClient.stream hands the pipeline reader. */
function sseResponse(events: object[]) {
  const encoder = new TextEncoder();
  const chunks = [
    encoder.encode(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("")),
  ];
  let i = 0;
  return {
    body: {
      getReader: () => ({
        read: async () =>
          i < chunks.length
            ? { value: chunks[i++], done: false }
            : { value: undefined, done: true },
      }),
    },
  };
}

const COMPLETE_EVENT = {
  type: "recon_complete",
  run_id: "r1",
  total_payouts: 3,
  total_deposits: 3,
  matched_count: 2,
  exception_count: 1,
  unmatched_count: 0,
  total_variance: "12.50",
  match_rate: "66.7",
};

beforeEach(() => {
  stream.mockReset();
});

describe("useReconPipeline — recon_complete cache invalidation (R4-A #3)", () => {
  it("invalidates recon-runs, recon-results, recon-bucket-summary AND recon-close-readiness", async () => {
    // The SSE pipeline path bypasses useCreateReconRun, so it must invalidate
    // the same caches itself: a NEW run changes the period's close scope and
    // readiness counts. Only invalidating recon-runs left a green-stale
    // CloseChecklist that could gate a close freezing the new run's
    // unreviewed rows.
    stream.mockResolvedValue(sseResponse([COMPLETE_EVENT]));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const { result } = renderHook(() => useReconPipeline(), {
      wrapper: makeWrapper(qc),
    });
    result.current.runPipeline({
      date_from: "2026-05-01",
      date_to: "2026-05-31",
    });
    await waitFor(() => expect(result.current.runId).toBe("r1"));

    const invalidatedKeys = invalidateSpy.mock.calls.map(
      ([filters]) => filters?.queryKey,
    );
    expect(invalidatedKeys).toContainEqual(["recon-runs"]);
    expect(invalidatedKeys).toContainEqual(["recon-results"]);
    expect(invalidatedKeys).toContainEqual(["recon-bucket-summary"]);
    expect(invalidatedKeys).toContainEqual(["recon-close-readiness"]);
  });

  it("does not invalidate caches on recon_error (nothing was written)", async () => {
    stream.mockResolvedValue(
      sseResponse([{ type: "recon_error", error: "sync failed" }]),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const { result } = renderHook(() => useReconPipeline(), {
      wrapper: makeWrapper(qc),
    });
    result.current.runPipeline({
      date_from: "2026-05-01",
      date_to: "2026-05-31",
    });
    await waitFor(() => expect(result.current.error).toBe("sync failed"));

    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});
