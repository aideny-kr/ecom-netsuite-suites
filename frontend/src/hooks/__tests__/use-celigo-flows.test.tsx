import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, expectTypeOf, it, vi } from "vitest";
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
  useCeligoScriptFamilies,
  useCeligoScriptFamily,
  type CeligoScriptFamilyKind,
  type CeligoScriptFamilySummary,
} from "@/hooks/use-celigo-flows";

// Residual fix 1 (build judge) -- `CeligoScriptFamilySummary.kind` was typed
// `string`, which let a typo (or a value the backend enum doesn't have)
// through tsc silently. The backend's `kind` is a closed six-value enum
// (spec §2.2) -- this type-level check locks the frontend field to the same
// closed union (`CeligoScriptFamilyKind`), exported so callers (e.g.
// `family-row.tsx`'s `KIND_BADGE` record) can key off it without re-typing
// the six literals. A mismatch here fails `tsc --noEmit`, not `vitest run`.
describe("CeligoScriptFamilySummary.kind — closed union (residual fix 1)", () => {
  it("kind is exactly CeligoScriptFamilyKind, not a bare string", () => {
    expectTypeOf<CeligoScriptFamilySummary["kind"]>().toEqualTypeOf<CeligoScriptFamilyKind>();
    expectTypeOf<CeligoScriptFamilyKind>().toEqualTypeOf<
      "hook" | "transform" | "filter" | "router" | "mixed" | "unattached"
    >();
  });
});

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

// Task 3 (Scripts view route params + hooks, spec §3.3) -- over Task 2's
// GET /api/v1/celigo/scripts/families[/{dedup_key}].
it("useCeligoScriptFamilies fetches GET /api/v1/celigo/scripts/families under ['celigo','script-families']", async () => {
  api.get.mockResolvedValueOnce({
    totals: {
      scripts: 0,
      families: 0,
      attached_families: 0,
      unattached_families: 0,
      diverged_families: 0,
      sites: 0,
      flows_with_sites: 0,
      flows_total: 0,
      integrations_with_sites: 0,
      sites_with_open_errors: 0,
    },
    families: [],
    synced_at: null,
  });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useCeligoScriptFamilies(), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/celigo/scripts/families");
  expect(qc.getQueryState(["celigo", "script-families"])).toBeDefined();
});

it("useCeligoScriptFamily fetches one family's detail by dedup_key", async () => {
  api.get.mockResolvedValueOnce({
    summary: { dedup_key: "fam1", name: "synthetic_family", kind: "hook" },
    members: [],
    versions: [],
    sites: [],
  });
  const qc = new QueryClient(qcOpts);
  const { result } = renderHook(() => useCeligoScriptFamily("fam1"), { wrapper: makeWrapper(qc) });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(api.get).toHaveBeenCalledWith("/api/v1/celigo/scripts/families/fam1");
});

it("useCeligoScriptFamily never calls the API when no dedup_key is given", () => {
  const qc = new QueryClient(qcOpts);
  renderHook(() => useCeligoScriptFamily(null), { wrapper: makeWrapper(qc) });
  expect(api.get).not.toHaveBeenCalled();
});
