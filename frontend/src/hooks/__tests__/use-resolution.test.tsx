import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";

const get = vi.fn();
vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: (...a: unknown[]) => get(...a),
    post: (...a: unknown[]) => Promise.resolve({}),
  },
}));

import { useNeedsHumanProposals, NEEDS_HUMAN_PROPOSALS_LIMIT } from "@/hooks/use-resolution";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue([]);
});

describe("useNeedsHumanProposals", () => {
  it("requests a high limit so the cross-group fetch is not silently truncated at the route's default of 100", async () => {
    const { result } = renderHook(() => useNeedsHumanProposals("r1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(NEEDS_HUMAN_PROPOSALS_LIMIT).toBe(1000);
    expect(get).toHaveBeenCalledWith(
      `/api/v1/reconciliation/runs/r1/resolution-groups/proposals?action=needs_human&limit=${NEEDS_HUMAN_PROPOSALS_LIMIT}`,
    );
  });
});
