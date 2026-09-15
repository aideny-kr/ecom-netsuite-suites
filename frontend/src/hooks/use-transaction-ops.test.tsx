import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import {
  useTransactionConfigs,
  useTransactionFindings,
  useTransactionDecision,
  useTransactionOperation,
  useStartTransactionRun,
  useRecheckTransactionOperation,
} from "./use-transaction-ops";
const context = vi.hoisted(() => ({
  flags: { celigo: true, reconciliation: true },
  tenant: "tenant-a",
  permission: true,
}));
vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}));
vi.mock("@/providers/auth-provider", () => ({
  useAuth: () => ({ user: { tenant_id: context.tenant } }),
}));
vi.mock("./use-features", () => ({
  useFeatures: () => ({ data: context.flags, isLoading: false, error: null }),
}));
vi.mock("./use-permissions", () => ({
  usePermissions: () => ({ hasPermission: () => context.permission }),
}));
function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: {
            queries: { retry: false },
            mutations: { retry: false },
          },
        })
      }
    >
      {children}
    </QueryClientProvider>
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  context.flags = { celigo: true, reconciliation: true };
  context.permission = true;
  context.tenant = "tenant-a";
});
describe("transaction operations API boundary", () => {
  it("requests a read-only recheck with a stable evaluation key and no write or actor payload", async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ id: "check" });
    const { result } = renderHook(() => useRecheckTransactionOperation(), { wrapper });
    const input = { id: "proposal/1", evaluation_key: "request-1" };
    await act(async () => {
      await result.current.mutateAsync(input);
      await result.current.mutateAsync(input);
    });
    expect(apiClient.post).toHaveBeenNthCalledWith(2,
      "/api/v1/transaction-ops/proposals/proposal%2F1/recheck",
      { evaluation_key: "request-1" },
    );
  });
  it("blocks outcome rechecks when permission is unavailable", async () => {
    context.permission = false;
    const { result } = renderHook(() => useRecheckTransactionOperation(), { wrapper });
    await act(async () => {
      await expect(result.current.mutateAsync({ id: "p", evaluation_key: "request-1" })).rejects.toThrow("Access unavailable");
    });
    expect(apiClient.post).not.toHaveBeenCalled();
  });
  it.each(["celigo", "reconciliation", "permission"])(
    "does not fetch without %s",
    async (flag) => {
      if (flag === "permission") context.permission = false;
      else context.flags[flag as "celigo" | "reconciliation"] = false;
      renderHook(() => useTransactionConfigs(), { wrapper });
      await new Promise((resolve) => setTimeout(resolve, 5));
      expect(apiClient.get).not.toHaveBeenCalled();
    },
  );
  it("reads finding pages with an explicit offset and bounded completeness sentinel", async () => {
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce(
        Array.from({ length: 100 }, (_, i) => ({ id: String(i) })),
      )
      .mockResolvedValueOnce([{ id: "next" }]);
    const { result } = renderHook(() => useTransactionFindings("run-1", 100), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiClient.get).toHaveBeenCalledWith(
      "/api/v1/transaction-ops/runs/run-1/findings?offset=100&limit=100",
    );
    expect(apiClient.get).toHaveBeenCalledWith(
      "/api/v1/transaction-ops/runs/run-1/findings?offset=200&limit=1",
    );
    expect(result.current.data?.hasNext).toBe(true);
  });
  it("uses the frozen fingerprint and authenticated API actor, not an actor from the page", async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      id: "p",
      status: "approved",
    });
    const { result } = renderHook(() => useTransactionDecision(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        id: "p",
        decision: "approve",
        evidence_fingerprint: "a".repeat(64),
        note: "Reviewed source",
      });
    });
    expect(apiClient.post).toHaveBeenCalledWith(
      "/api/v1/transaction-ops/proposals/p/decision",
      {
        decision: "approve",
        evidence_fingerprint: "a".repeat(64),
        note: "Reviewed source",
      },
    );
  });
  it("retains the caller's evaluation key across a retry and fixes origin to manual", async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ id: "run" });
    const { result } = renderHook(() => useStartTransactionRun(), { wrapper });
    const input = {
      configId: "cfg",
      evaluation_key: "request-1",
      order_references: ["R123456789-EU"],
    };
    await act(async () => {
      await result.current.mutateAsync(input);
      await result.current.mutateAsync(input);
    });
    expect(apiClient.post).toHaveBeenNthCalledWith(
      2,
      "/api/v1/transaction-ops/configs/cfg/runs",
      {
        origin: "manual",
        evaluation_key: "request-1",
        order_references: ["R123456789-EU"],
      },
    );
  });
  it("fetches the operation ledger only for approved proposals", async () => {
    const { rerender } = renderHook(
      ({ status }) => useTransactionOperation("p", status),
      { wrapper, initialProps: { status: "pending" } },
    );
    expect(apiClient.get).not.toHaveBeenCalled();
    vi.mocked(apiClient.get).mockResolvedValue(null);
    rerender({ status: "approved" });
    await waitFor(() =>
      expect(apiClient.get).toHaveBeenCalledWith(
        "/api/v1/transaction-ops/proposals/p/operation",
      ),
    );
  });
});
