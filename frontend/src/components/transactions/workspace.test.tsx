import React from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { TransactionWorkspace } from "./workspace";
const mocks = vi.hoisted(() => ({
  configs: [
    {
      id: "scope-a",
      name: "Framework Inc",
      enabled: true,
      schedule_enabled: true,
      interval_minutes: 1440,
      tenant_id: "tenant-a",
    },
  ],
  allowed: true,
}));
vi.mock("@/hooks/use-transaction-ops", () => ({
  useTransactionAccess: () => ({
    allowed: mocks.allowed,
    tenantId: "tenant-a",
    loading: false,
  }),
  useTransactionConfigs: () => ({ data: mocks.configs, isLoading: false }),
  useTransactionDecision: () => ({ mutateAsync: vi.fn() }),
}));
vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}));
vi.mock("./orders-page", () => ({
  OrdersPage: () => <div>Imported source orders</div>,
}));
const run = {
  id: "review-a",
  config_id: "scope-a",
  created_at: "2026-09-08T10:00:00Z",
  status: "running",
  params_json: {
    review: {
      id: "span-a",
      start: "2026-08-31T07:00:00Z",
      end: "2026-09-07T07:00:00Z",
    },
  },
};
const balance = {
  status: "difference",
  currency: "USD",
  amounts: {
    order_total: {
      source: "123456789012345.123456",
      target: "10.00",
      delta: "123456789012335.123456",
    },
  },
};
beforeEach(() => {
  vi.clearAllMocks();
  mocks.allowed = true;
  vi.mocked(apiClient.get).mockImplementation(async (path: string) => {
    if (path.includes("/review/findings"))
      return {
        summary: { checked: 8, matched: 5, needs_review: 2, not_verified: 1 },
        items: [
          {
            id: "f1",
            run_id: run.id,
            order_reference: "R123456789",
            case_id: "case-a",
            balance,
          },
        ],
        total: 8,
        has_next: true,
      } as never;
    if (path.endsWith("/review"))
      return {
        complete: false,
        status: "running",
        completed_slices: 1,
        period_start: run.params_json.review.start,
        period_end: run.params_json.review.end,
      } as never;
    if (path.includes("/runs?")) return [run] as never;
    if (path.includes("/cases?"))
      return [
        {
          id: "case-a",
          order_reference: "R123456789",
          status: "open",
          scope_json: { subsidiary_id: "1" },
          latest_report_json: { balance },
          last_observed_at: "2026-09-08T10:00:00Z",
        },
      ] as never;
    if (path.includes("/proposals?")) return [] as never;
    throw new Error(`Unexpected ${path}`);
  });
});
function mount() {
  return render(
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
      <TransactionWorkspace />
    </QueryClientProvider>,
  );
}
it("renders server totals and exact evidence without claiming a complete financial reconciliation", async () => {
  mount();
  expect(await screen.findByText("R123456789")).toBeInTheDocument();
  expect(screen.getByTestId("stat-checked")).toHaveTextContent("8");
  expect(screen.getByText("123456789012345.123456")).toBeInTheDocument();
  expect(
    screen.getByText(/Replica freshness is unverified/),
  ).toBeInTheDocument();
  expect(screen.queryByText("Financially reconciled")).not.toBeInTheDocument();
});
it("queues selected cases as one read/proposal batch and never submits approval", async () => {
  vi.mocked(apiClient.post).mockResolvedValue({
    runs: [{ id: "batch-run", config_id: "scope-a", case_ids: ["case-a"] }],
    blocked: [],
  });
  mount();
  fireEvent.click(screen.getByRole("tab", { name: "Cases" }));
  fireEvent.click(await screen.findByLabelText("Select case R123456789"));
  fireEvent.click(screen.getByRole("button", { name: /Investigate selected/ }));
  await waitFor(() =>
    expect(apiClient.post).toHaveBeenCalledWith(
      "/api/v1/transaction-ops/cases/investigate",
      { case_ids: ["case-a"], evaluation_key: expect.any(String) },
    ),
  );
  expect(
    vi.mocked(apiClient.post).mock.calls.some(([p]) => p.includes("decision")),
  ).toBe(false);
});
it("keeps failed evidence loading distinct from zero matches", async () => {
  vi.mocked(apiClient.get).mockImplementation(async () => {
    throw new Error("Unavailable");
  });
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    /could not|unavailable/i,
  );
  expect(screen.getByTestId("stat-checked")).toHaveTextContent("—");
});
it("reuses a failed period request key but gives an intentional new review a fresh key", async () => {
  vi.mocked(apiClient.post)
    .mockRejectedValueOnce(new Error("Temporary outage"))
    .mockResolvedValue(run);
  mount();
  const submit = screen.getByRole("button", { name: "Reconcile period" });
  fireEvent.click(submit);
  await waitFor(() => expect(submit).not.toBeDisabled());
  fireEvent.click(submit);
  await waitFor(() => expect(apiClient.post).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(submit).not.toBeDisabled());
  const first = vi.mocked(apiClient.post).mock.calls[0][1] as {
    evaluation_key: string;
  };
  expect(vi.mocked(apiClient.post).mock.calls[1][1]).toEqual(first);
  fireEvent.click(submit);
  await waitFor(() => expect(apiClient.post).toHaveBeenCalledTimes(3));
  expect(
    (vi.mocked(apiClient.post).mock.calls[2][1] as typeof first).evaluation_key,
  ).not.toBe(first.evaluation_key);
});
it("can open a historical period whose scope has since been replaced", async () => {
  const original = vi.mocked(apiClient.get).getMockImplementation()!;
  vi.mocked(apiClient.get).mockImplementation(async (path: string) =>
    path.includes("/runs?")
      ? ([{ ...run, config_id: "retired-scope" }] as never)
      : original(path),
  );
  mount();
  fireEvent.click(screen.getByRole("tab", { name: "Run history" }));
  fireEvent.click(await screen.findByRole("button", { name: "View period" }));
  expect(await screen.findByText("R123456789")).toBeInTheDocument();
});

it("keeps a revised entity's existing period visible with its current entity name", async () => {
  const original = vi.mocked(apiClient.get).getMockImplementation()!;
  const config = mocks.configs[0];
  const scope = {
    source_connection_id: "source-a",
    source_step_id: null,
    netsuite_account_id: "ACCOUNT_SB1",
    subsidiary_id: "2",
    record_type: "salesOrder",
  };
  mocks.configs[0] = { ...config, ...scope };
  vi.mocked(apiClient.get).mockImplementation(async (path: string) =>
    path.includes("/runs?")
      ? ([{
          ...run,
          tenant_id: "tenant-a",
          config_id: "retired-scope",
          config_snapshot: { ...scope, netsuite_account_id: "account-sb1" },
        }] as never)
      : original(path),
  );
  try {
    mount();
    expect(await screen.findByText("R123456789")).toBeInTheDocument();
    expect(screen.getByTestId("stat-checked")).toHaveTextContent("8");
    expect(screen.getByText(/Framework Inc · USD/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Review entity"), { target: { value: "scope-a" } });
    expect(await screen.findByText("R123456789")).toBeInTheDocument();
    expect(apiClient.get).toHaveBeenCalledWith(expect.stringContaining(`/runs/${run.id}/review/findings`));
  } finally {
    mocks.configs[0] = config;
  }
});
it("preserves source-order browsing when reconciliation access is unavailable", () => {
  mocks.allowed = false;
  mount();
  expect(screen.getByText("Imported source orders")).toBeInTheDocument();
  expect(apiClient.get).not.toHaveBeenCalled();
});
