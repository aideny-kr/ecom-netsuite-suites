import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const get = vi.fn();
const post = vi.fn();
const patch = vi.fn();
const del = vi.fn();

vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: (...a: unknown[]) => get(...a),
    post: (...a: unknown[]) => post(...a),
    patch: (...a: unknown[]) => patch(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}));

import {
  useLearnedRules,
  useCreateLearnedRule,
  useUpdateLearnedRule,
  useDeleteLearnedRule,
} from "@/hooks/use-learned-rules";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  patch.mockReset();
  del.mockReset();
});

describe("use-learned-rules", () => {
  it("useLearnedRules GETs the collection endpoint", async () => {
    const rules = [{ id: "r1", rule_description: "x", is_active: true }];
    get.mockResolvedValue(rules);

    const { result } = renderHook(() => useLearnedRules(), { wrapper });

    await waitFor(() => expect(result.current.data).toEqual(rules));
    expect(get).toHaveBeenCalledWith("/api/v1/learned-rules");
  });

  it("useCreateLearnedRule POSTs the payload to the collection", async () => {
    post.mockResolvedValue({ id: "new" });
    const { result } = renderHook(() => useCreateLearnedRule(), { wrapper });

    await result.current.mutateAsync({ rule_description: "count by class", rule_category: "query_logic" });

    expect(post).toHaveBeenCalledWith("/api/v1/learned-rules", {
      rule_description: "count by class",
      rule_category: "query_logic",
    });
  });

  it("useUpdateLearnedRule PATCHes the item by id", async () => {
    patch.mockResolvedValue({ id: "r1", is_active: false });
    const { result } = renderHook(() => useUpdateLearnedRule(), { wrapper });

    await result.current.mutateAsync({ id: "r1", is_active: false });

    expect(patch).toHaveBeenCalledWith("/api/v1/learned-rules/r1", { is_active: false });
  });

  it("useDeleteLearnedRule DELETEs the item by id", async () => {
    del.mockResolvedValue(undefined);
    const { result } = renderHook(() => useDeleteLearnedRule(), { wrapper });

    await result.current.mutateAsync("r1");

    expect(del).toHaveBeenCalledWith("/api/v1/learned-rules/r1");
  });
});
