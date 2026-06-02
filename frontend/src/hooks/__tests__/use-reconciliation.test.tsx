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

import { useApproveBucket } from "@/hooks/use-reconciliation";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  patch.mockReset();
  post.mockReset();
  get.mockReset();
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
