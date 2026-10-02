import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FreshnessAlertBanner } from "./freshness-alert-banner";
import { ApiError } from "@/lib/api-client";
import type { OperationalEntity, OperationalStatus } from "./operational-status-types";

const mocks = vi.hoisted(() => ({ get: vi.fn(), access: { allowed: true, tenantId: "tenant-a" } }));
vi.mock("@/hooks/use-transaction-ops", () => ({ useTransactionAccess: () => mocks.access }));
vi.mock("@/lib/api-client", async original => ({ ...await original<typeof import("@/lib/api-client")>(), apiClient: { get: mocks.get } }));

function entity(state: "alert" | "healthy" | "within_grace" = "alert", config_id = "au"): OperationalEntity {
  return {
    config_id, name: "Framework AU → NetSuite",
    coverage: { status: "behind", checked_through: "2026-09-29", expected_checked_through: "2026-09-30" },
    schedule: { enabled: true, kind: "daily", timezone: "America/Los_Angeles", next_check_at: null },
    active_runs: [], active_runs_truncated: false, latest_schedule: null,
    next_action: { kind: "scheduled_check", reason: null, eligible_at: null, dispatch_verified: false },
    freshness: { state, reason: "daily_scan_stopped", deadline_at: "2026-10-02T00:00:00Z", grace_hours: 8 },
  };
}
function snapshot(entities = [entity()], truncated = false): OperationalStatus {
  return { observed_at: "2026-10-01T17:00:00Z", source: "stored_reconciliation_state", entities, truncated, next_offset: truncated ? 50 : null };
}
function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0, gcTime: 0 } } });
  const tree = () => <QueryClientProvider client={client}><FreshnessAlertBanner /></QueryClientProvider>;
  const view = render(tree());
  return { ...view, refreshTree: () => view.rerender(tree()), client };
}
beforeEach(() => {
  vi.clearAllMocks();
  mocks.access = { allowed: true, tenantId: "tenant-a" };
  mocks.get.mockResolvedValue(snapshot());
});
afterEach(() => vi.useRealTimers());

it("shows the entity, completed and expected dates, and a working Ops status link", async () => {
  setup();
  expect(await screen.findByText(/Verified through 2026-09-29; expected 2026-09-30/)).toBeVisible();
  expect(screen.getByRole("link", { name: "Ops status" })).toHaveAttribute("href", "/settings/ops-status");
  expect(mocks.get).toHaveBeenCalledExactlyOnceWith("/api/v1/transaction-ops/operational-status?daily_only=true&limit=50");
});

it("clears the same standing alert after the next poll proves coverage caught up", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  setup();
  await screen.findByText(/Daily reconciliation needs attention/);
  mocks.get.mockResolvedValue(snapshot([entity("healthy")]));
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  await waitFor(() => expect(screen.queryByLabelText("Reconciliation freshness")).not.toBeInTheDocument());
  expect(mocks.get).toHaveBeenCalledTimes(2);
});

it("does not warn about healthy or normal running work within its grace period", async () => {
  mocks.get.mockResolvedValue(snapshot([entity("healthy", "inc"), entity("within_grace")]));
  setup();
  await waitFor(() => expect(mocks.get).toHaveBeenCalledOnce());
  expect(screen.queryByLabelText("Reconciliation freshness")).not.toBeInTheDocument();
});

it("discloses a bounded partial check instead of claiming every entity is healthy", async () => {
  mocks.get.mockResolvedValue(snapshot([entity("healthy")], true));
  setup();
  expect(await screen.findByText(/first 50 enabled daily schedules/)).toBeVisible();
});

it("does not request or display cached evidence after permissions are lost", async () => {
  const view = setup();
  await screen.findByText(/Framework AU/);
  mocks.access.allowed = false;
  view.refreshTree();
  expect(screen.queryByText(/Framework AU/)).not.toBeInTheDocument();
  expect(mocks.get).toHaveBeenCalledOnce();
});

it("does not carry another tenant's alert across a workspace switch", async () => {
  const view = setup();
  await screen.findByText(/Framework AU/);
  mocks.access.tenantId = "tenant-b";
  mocks.get.mockReturnValue(new Promise(() => {}));
  view.refreshTree();
  expect(screen.queryByText(/Framework AU/)).not.toBeInTheDocument();
});

it("labels a failed refresh as stale and never hides failure behind a healthy snapshot", async () => {
  const view = setup();
  await screen.findByText(/Framework AU/);
  mocks.get.mockRejectedValue(new Error("offline"));
  await act(async () => { await view.client.refetchQueries(); });
  expect(await screen.findByText(/These dates are from the last saved snapshot/)).toBeVisible();
});

it("removes protected cached evidence on a 403 without retrying the denied request", async () => {
  const view = setup();
  await screen.findByText(/Framework AU/);
  mocks.get.mockRejectedValue(new ApiError("forbidden", 403));
  await act(async () => { await view.client.refetchQueries(); });
  await waitFor(() => expect(screen.queryByText(/Framework AU/)).not.toBeInTheDocument());
  expect(mocks.get).toHaveBeenCalledTimes(2);
});

it("also removes a previous failed-check warning when reconciliation access is revoked", async () => {
  mocks.get.mockRejectedValue(new Error("offline"));
  const view = setup();
  await screen.findByText(/Freshness check failed/);
  mocks.access.allowed = false;
  view.refreshTree();
  expect(screen.queryByLabelText("Reconciliation freshness")).not.toBeInTheDocument();
});
