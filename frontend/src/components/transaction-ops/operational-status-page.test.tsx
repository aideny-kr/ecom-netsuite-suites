import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { OperationalStatusPage } from "./operational-status-page";
import { OpsStatusLink } from "./ops-status-link";
import type { OperationalEntity, OperationalStatus } from "./operational-status-types";
import { ApiError } from "@/lib/api-client";

const mocks = vi.hoisted(() => ({ get: vi.fn(), access: { allowed: true, tenantId: "tenant-a", loading: false, error: null } }));
vi.mock("@/hooks/use-transaction-ops", () => ({ useTransactionAccess: () => mocks.access }));
vi.mock("@/lib/api-client", async importOriginal => ({ ...await importOriginal<typeof import("@/lib/api-client")>(), apiClient: { get: mocks.get } }));

function entity(overrides: Partial<OperationalEntity> = {}): OperationalEntity {
  return {
    config_id: "example", name: "Example entity",
    coverage: { status: "up_to_date", checked_through: "2026-09-27", expected_checked_through: "2026-09-27" },
    schedule: { enabled: true, kind: "daily", timezone: "America/Los_Angeles", next_check_at: "2026-09-29T16:00:00Z" },
    active_runs: [], active_runs_truncated: false, latest_schedule: null,
    next_action: { kind: "scheduled_check", reason: "waiting_for_daily_cutoff", eligible_at: "2026-09-29T16:00:00Z", dispatch_verified: false },
    ...overrides,
  };
}
function status(overrides: Partial<OperationalStatus> = {}): OperationalStatus {
  return { observed_at: "2026-09-28T17:22:00Z", source: "stored_reconciliation_state", entities: [entity()], truncated: false, next_offset: null, ...overrides };
}
function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0, gcTime: 0 } } });
  const tree = () => <QueryClientProvider client={client}><OpsStatusLink /><OperationalStatusPage /></QueryClientProvider>;
  const result = render(tree());
  return { ...result, refreshTree: () => result.rerender(tree()) };
}
beforeEach(() => {
  vi.clearAllMocks();
  mocks.access = { allowed: true, tenantId: "tenant-a", loading: false, error: null };
  mocks.get.mockResolvedValue(status());
});

it("opens through Settings and displays coverage without promising accounting completion or dispatch", async () => {
  setup();
  expect(screen.getByRole("link", { name: /Ops status/ })).toHaveAttribute("href", "/transaction-operations/status");
  expect(await screen.findByText("Caught up")).toBeVisible();
  expect(screen.getByText("Expected through 2026-09-27")).toBeVisible();
  expect(screen.getByText(/eligibility is not a dispatch confirmation/)).toBeVisible();
  expect(screen.getByText(/Scan coverage is separate from accounting completion/)).toBeVisible();
  expect(mocks.get).toHaveBeenCalledExactlyOnceWith("/api/v1/transaction-ops/operational-status?limit=20&offset=0");
});

it("does not expose the link or request status without existing reconciliation access", () => {
  mocks.access.allowed = false;
  setup();
  expect(screen.queryByRole("link", { name: /Ops status/ })).not.toBeInTheDocument();
  expect(mocks.get).not.toHaveBeenCalled();
});

it("never shows another tenant's cached snapshot while switching workspaces", async () => {
  const view = setup();
  await screen.findByText("Example entity");
  mocks.access.tenantId = "tenant-b";
  mocks.get.mockReturnValue(new Promise(() => {}));
  view.refreshTree();
  expect(screen.queryByText("Example entity")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Loading reconciliation status");
});

it("marks a retained snapshot as older after a failed refresh", async () => {
  setup();
  await screen.findByText("Example entity");
  mocks.get.mockRejectedValue(new Error("network unavailable"));
  fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("snapshot below is older");
  expect(screen.getByText("Example entity")).toBeVisible();
});

it("hides cached evidence when the API denies access", async () => {
  setup();
  await screen.findByText("Example entity");
  mocks.get.mockRejectedValue(new ApiError("Forbidden", 403));
  fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("no longer have access");
  expect(screen.queryByText("Example entity")).not.toBeInTheDocument();
  expect(mocks.get).toHaveBeenCalledTimes(2);
});

it("follows server pagination and resets it on tenant change", async () => {
  mocks.get.mockResolvedValueOnce(status({ truncated: true, next_offset: 20 })).mockResolvedValueOnce(status({ entities: [entity({ name: "Second page" })] }));
  const view = setup();
  await screen.findByText("Example entity");
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  await screen.findByText("Second page");
  expect(mocks.get).toHaveBeenLastCalledWith("/api/v1/transaction-ops/operational-status?limit=20&offset=20");
  expect(screen.queryByText("Example entity")).not.toBeInTheDocument();
  mocks.access.tenantId = "tenant-b";
  view.refreshTree();
  await waitFor(() => expect(mocks.get).toHaveBeenLastCalledWith("/api/v1/transaction-ops/operational-status?limit=20&offset=0"));
});

it("shows an explicit empty state without an all-clear claim", async () => {
  mocks.get.mockResolvedValue(status({ entities: [] }));
  setup();
  expect(await screen.findByText("No reconciliation configurations are available on this page.")).toBeVisible();
  expect(screen.queryByText("Caught up")).not.toBeInTheDocument();
});

it("preserves unknown financial counts and blocking read evidence", async () => {
  const run = {
    run_id: "run-one", origin: "schedule", execution_state: "running", phase: "destination",
    run_state_updated_at: null,
    financial_counts: { scope: "run_checkpoint" as const, matched: null, needs_review: 2, not_verified: 1 },
    last_read_failure: { code: "read_timeout", stage: "dependency_page", resolved: false, blocking: true },
    collection_wait: { basis: "recorded_wait_requires_scheduler_recheck", owner: null },
  };
  mocks.get.mockResolvedValue(status({ entities: [entity({
    coverage: { status: "not_verified", checked_through: null, expected_checked_through: "2026-09-27" },
    active_runs: [run], latest_schedule: run,
  })] }));
  setup();
  expect(await screen.findByText(/A read issue is blocking an active run/)).toBeVisible();
  expect(screen.queryByText("Caught up")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("Run evidence and read issues"));
  expect(screen.getByText("Unknown matched · 2 need review · 1 unverified")).toBeInTheDocument();
  expect(screen.getByText(/Counts describe this run checkpoint, not period totals/)).toBeInTheDocument();
  expect(screen.getByText(/A collection wait was recorded/)).toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "View run" })).toHaveLength(1);
});
