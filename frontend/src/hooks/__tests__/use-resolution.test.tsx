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

import {
  useGroupProposals,
  useNeedsHumanProposals,
  NEEDS_HUMAN_PROPOSALS_LIMIT,
} from "@/hooks/use-resolution";

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

describe("useGroupProposals", () => {
  it("omits the currency param and keeps the existing URL when currency is not passed (unchanged behavior)", async () => {
    const { result } = renderHook(
      () => useGroupProposals("r1", "fees:book_fee_line:deposit"),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(get).toHaveBeenCalledWith(
      "/api/v1/reconciliation/runs/r1/resolution-groups/fees%3Abook_fee_line%3Adeposit/proposals",
    );
  });

  it("narrows the panel's item fetch to the expanded group's own currency", async () => {
    const { result } = renderHook(
      () => useGroupProposals("r1", "fees:book_fee_line:deposit", "EUR"),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(get).toHaveBeenCalledWith(
      "/api/v1/reconciliation/runs/r1/resolution-groups/fees%3Abook_fee_line%3Adeposit/proposals?currency=EUR",
    );
  });

  it("varies the query key by currency so two currencies of the same group_key each fetch independently, not from a shared cache entry", async () => {
    get.mockResolvedValueOnce(["usd-data"]).mockResolvedValueOnce(["eur-data"]);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const localWrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );

    const { result, rerender } = renderHook(
      ({ currency }: { currency: string }) =>
        useGroupProposals("r1", "fees:book_fee_line:deposit", currency),
      { wrapper: localWrapper, initialProps: { currency: "USD" } },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(["usd-data"]);

    rerender({ currency: "EUR" });
    await waitFor(() => expect(result.current.data).toEqual(["eur-data"]));

    // Both currencies actually hit the network — proves the query key (and
    // therefore the cache entry) differs by currency instead of the EUR
    // render reusing the USD group_key's cached result.
    expect(get).toHaveBeenCalledTimes(2);
  });
});
