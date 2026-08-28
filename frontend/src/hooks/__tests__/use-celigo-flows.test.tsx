import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

// Task 9 — TanStack Query hooks over Task 8's read-only flow-map endpoints
// (backend/app/api/v1/celigo_flows.py). Mirrors use-dashboard.test.tsx's
// pattern: mock apiClient only, exercise the real hooks.

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), delete: vi.fn(), post: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

import {
  useCeligoIntegrations,
  useCeligoIntegrationFlows,
  useCeligoAllFlows,
  useCeligoFlowDetail,
  useCeligoSyncStatus,
} from "@/hooks/use-celigo-flows";

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

const qcOpts = { defaultOptions: { queries: { retry: false }, mutations: { retry: false } } };

beforeEach(() => {
  api.get.mockReset();
});

it("useCeligoIntegrations fetches GET /api/v1/celigo/integrations under ['celigo','integrations']", async () => {
  api.get.mockResolvedValueOnce([]);
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useCeligoIntegrations(), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/celigo/integrations");
  expect(qc.getQueryState(["celigo", "integrations"])).toBeDefined();
});

it("useCeligoIntegrationFlows fetches the given integration's flows", async () => {
  api.get.mockResolvedValueOnce([{ id: "f-1", name: "Sales Order Sync" }]);
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useCeligoIntegrationFlows("int-1"), {
    wrapper: makeWrapper(qc),
  });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/celigo/integrations/int-1/flows");
});

it("useCeligoIntegrationFlows never calls the API when no integration id is given", () => {
  const qc = new QueryClient(qcOpts);
  renderHook(() => useCeligoIntegrationFlows(undefined), { wrapper: makeWrapper(qc) });
  expect(api.get).not.toHaveBeenCalled();
});

it("useCeligoFlowDetail fetches one flow's detail by id", async () => {
  api.get.mockResolvedValueOnce({ id: "f-1", steps: [] });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useCeligoFlowDetail("f-1"), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/celigo/flows/f-1");
});

it("useCeligoFlowDetail never calls the API when no flow id is given", () => {
  const qc = new QueryClient(qcOpts);
  renderHook(() => useCeligoFlowDetail(undefined), { wrapper: makeWrapper(qc) });
  expect(api.get).not.toHaveBeenCalled();
});

it("useCeligoAllFlows fires one parallel query per integration id and combines the results", async () => {
  api.get.mockImplementation((path: string) => {
    if (path === "/api/v1/celigo/integrations/int-1/flows") {
      return Promise.resolve([{ id: "f-1", name: "Flow One" }]);
    }
    if (path === "/api/v1/celigo/integrations/int-2/flows") {
      return Promise.resolve([{ id: "f-2", name: "Flow Two" }]);
    }
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useCeligoAllFlows(["int-1", "int-2"]), {
    wrapper: makeWrapper(qc),
  });
  await waitFor(() => expect(result.current.every((q) => q.isSuccess)).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/celigo/integrations/int-1/flows");
  expect(api.get).toHaveBeenCalledWith("/api/v1/celigo/integrations/int-2/flows");
  expect(result.current[0].data).toEqual([{ id: "f-1", name: "Flow One" }]);
  expect(result.current[1].data).toEqual([{ id: "f-2", name: "Flow Two" }]);
});

it("useCeligoAllFlows shares its cache key with useCeligoIntegrationFlows for the same id", async () => {
  api.get.mockResolvedValue([{ id: "f-1" }]);
  const qc = new QueryClient(qcOpts);
  renderHook(() => useCeligoAllFlows(["int-1"]), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(qc.getQueryState(["celigo", "integration-flows", "int-1"])).toBeDefined());
});

// Fix round 1 -- optional addition (team lead: "you MAY now wire that stat
// in if it is cheap"). Task 8 added GET /celigo/sync-status for the
// mockup's "Last synced" stat that Task 9 originally had to drop.
it("useCeligoSyncStatus fetches GET /api/v1/celigo/sync-status under ['celigo','sync-status']", async () => {
  api.get.mockResolvedValueOnce({ last_synced_at: "2026-08-27T12:00:00Z" });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useCeligoSyncStatus(), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/celigo/sync-status");
  expect(qc.getQueryState(["celigo", "sync-status"])).toBeDefined();
});
