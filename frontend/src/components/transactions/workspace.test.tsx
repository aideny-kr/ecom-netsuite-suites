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
      source_connection_id: "source-a",
      source_step_id: null,
      netsuite_account_id: "123",
      subsidiary_id: "1",
      record_type: "salesorder",
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
  apiClient: { get: vi.fn(), post: vi.fn(), download: vi.fn() },
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
    if (path.includes("/case-groups?"))
      return {
        groups: [
          {
            group_id: "a".repeat(32),
            pattern: "Tax differences",
            case_count: 2,
            currency: "USD",
            scope: {
              source_connection_id: "source-a",
              source_step_id: null,
              netsuite_account_id: "123",
              subsidiary_id: "1",
              record_type: "salesorder",
            },
            order_total: "zero",
            tax: "negative",
            refunds: "zero",
            target_state: "fulfilled",
          },
        ],
        has_next: false,
        total_groups: 1,
        total_cases: 2,
      } as never;
    if (path.includes("/review-results?"))
      return {
        summary: { checked: 8, matched: 5, needs_review: 2, not_verified: 1 },
        items: [
          {
            id: "f1",
            run_id: run.id,
            review_run_id: run.id,
            config_id: run.config_id,
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
    if (path.includes("view=runs"))
      return { items: [run], total: 1, has_next: false } as never;
    if (path.includes("view=cases"))
      return {
        items: [
          {
            id: "case-a",
            order_reference: "R123456789",
            status: "open",
            scope_json: { subsidiary_id: "1" },
            latest_report_json: { balance },
            last_observed_at: "2026-09-08T10:00:00Z",
          },
        ],
        total: 1,
        has_next: false,
      } as never;
    if (path.includes("view=proposals"))
      return { items: [], total: 0, has_next: false } as never;
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
  expect(screen.getByText("+123456789012335.123456")).toBeInTheDocument();
  expect(screen.queryByText("123456789012345.123456")).not.toBeInTheDocument();
  expect(
    screen.getByRole("columnheader", { name: /Variance/ }),
  ).toBeInTheDocument();
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
  expect(
    (await screen.findAllByRole("alert")).some((node) =>
      /could not|unavailable/i.test(node.textContent || ""),
    ),
  ).toBe(true);
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
      ? ([
          {
            ...run,
            tenant_id: "tenant-a",
            config_id: "retired-scope",
            config_snapshot: { ...scope, netsuite_account_id: "account-sb1" },
          },
        ] as never)
      : original(path),
  );
  try {
    mount();
    expect(await screen.findByText("R123456789")).toBeInTheDocument();
    expect(screen.getByTestId("stat-checked")).toHaveTextContent("8");
    expect(screen.getByText(/Framework Inc · USD/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Review entity"), {
      target: { value: "scope-a" },
    });
    expect(await screen.findByText("R123456789")).toBeInTheDocument();
    expect(apiClient.get).toHaveBeenCalledWith(
      expect.stringContaining(`review_run_ids=${run.id}`),
    );
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

it("launches one group investigation with all-member pagination and exact scope", async () => {
  mount();
  const link = await screen.findByRole("link", { name: "Prepare group fixes →" });
  const prompt = new URL(
    link.getAttribute("href")!,
    "https://example.test",
  ).searchParams.get("compose")!;
  expect(prompt).toContain("transaction_ops.accounting_group");
  expect(prompt).toContain("show every unsupported case separately");
  expect(prompt).toContain("bounded concurrency and per-order verification and audit");
  expect(prompt).toContain("Do not treat this request or the group ID as financial approval");
  expect(prompt).toContain('"review_run_ids":["review-a"]');
  expect(prompt).toContain('"status":"needs_review"');
  expect(apiClient.get).toHaveBeenCalledWith(
    expect.stringContaining("review_run_ids=review-a&status=needs_review"),
  );
  fireEvent.change(screen.getByLabelText("Result status"), {
    target: { value: "not_verified" },
  });
  await waitFor(() =>
    expect(apiClient.get).toHaveBeenCalledWith(
      expect.stringContaining("review_run_ids=review-a&status=not_verified"),
    ),
  );
  fireEvent.change(screen.getByLabelText("Search order number"), {
    target: { value: "R123" },
  });
  await waitFor(() =>
    expect(apiClient.get).toHaveBeenCalledWith(
      expect.stringContaining("status=not_verified&search=R123"),
    ),
  );
  fireEvent.change(screen.getByLabelText("Result status"), {
    target: { value: "matched" },
  });
  expect(
    screen.queryByRole("region", { name: "Issue groups" }),
  ).not.toBeInTheDocument();
  expect(apiClient.post).not.toHaveBeenCalled();
});

it("uses 50/100/500 globally and resets pagination when filters change", async () => {
  const original = vi.mocked(apiClient.get).getMockImplementation()!;
  vi.mocked(apiClient.get).mockImplementation(async (path: string) => {
    const data = await original(path);
    if (path.includes("/review-results?"))
      return { ...(data as object), total: 1202 } as never;
    return data;
  });
  mount();
  expect(
    await screen.findByText("Showing 1–50 of 1202 orders"),
  ).toBeInTheDocument();
  expect(screen.getByLabelText("orders per page")).toHaveValue("50");
  expect(
    Array.from(
      (screen.getByLabelText("orders per page") as HTMLSelectElement).options,
    ).map((o) => o.value),
  ).toEqual(["50", "100", "500"]);
  fireEvent.click(screen.getAllByRole("button", { name: "Next" })[0]);
  await waitFor(() =>
    expect(apiClient.get).toHaveBeenCalledWith(
      expect.stringContaining("offset=50&limit=50"),
    ),
  );
  fireEvent.change(screen.getByLabelText("orders per page"), {
    target: { value: "100" },
  });
  await waitFor(() =>
    expect(apiClient.get).toHaveBeenCalledWith(
      expect.stringContaining("offset=0&limit=100"),
    ),
  );
  fireEvent.change(screen.getByLabelText("orders per page"), {
    target: { value: "500" },
  });
  await waitFor(() =>
    expect(apiClient.get).toHaveBeenCalledWith(
      expect.stringContaining("offset=0&limit=500"),
    ),
  );
  fireEvent.click(screen.getAllByRole("button", { name: "Next" })[0]);
  await waitFor(() =>
    expect(apiClient.get).toHaveBeenCalledWith(
      expect.stringContaining("offset=500&limit=500"),
    ),
  );
  fireEvent.change(screen.getByLabelText("Result status"), {
    target: { value: "needs_review" },
  });
  await waitFor(() =>
    expect(apiClient.get).toHaveBeenCalledWith(
      expect.stringContaining("offset=0&limit=500&status=needs_review"),
    ),
  );
  fireEvent.change(screen.getByLabelText("groups per page"), {
    target: { value: "500" },
  });
  await waitFor(() =>
    expect(apiClient.get).toHaveBeenCalledWith(
      expect.stringContaining("case-groups?limit=500&offset=0"),
    ),
  );
});

it("exports the full filter scope without including pagination parameters", async () => {
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => {});
  const create = vi.fn(() => "blob:report");
  const revoke = vi.fn();
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }),
  );
  vi.mocked(apiClient.download).mockResolvedValue(
    new Response(new Blob(["workbook"]), {
      headers: {
        "content-type":
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "content-disposition":
          'attachment; filename="reconciliation-report.xlsx"',
      },
    }),
  );
  try {
    mount();
    await screen.findByText("R123456789");
    fireEvent.change(screen.getByLabelText("Result status"), {
      target: { value: "needs_review" },
    });
    fireEvent.change(screen.getByLabelText("Search order number"), {
      target: { value: "R123" },
    });
    const button = screen.getByRole("button", { name: "Download Excel" });
    await waitFor(() => expect(button).not.toBeDisabled());
    fireEvent.click(button);
    await waitFor(() =>
      expect(apiClient.download).toHaveBeenCalledWith(
        "/api/v1/transaction-ops/review-export",
        { review_run_ids: [run.id], status: "needs_review", search: "R123" },
      ),
    );
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(create).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("blob:report"));
    expect(apiClient.post).not.toHaveBeenCalled();
  } finally {
    click.mockRestore();
    vi.unstubAllGlobals();
  }
});
