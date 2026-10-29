import React from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
it("shows each scope's own reason when a period cannot be queued", async () => {
  // Staging 2026-10-24: the per-scope messages were formatted again as an error object, so every
  // failure read "The request could not be confirmed" whatever the server said.
  vi.mocked(apiClient.post).mockRejectedValue(Object.assign(new Error("Conflict"), { status: 409 }));
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile period" }));
  const alert = await screen.findByText(/Review data could not be loaded or queued/);
  expect(alert).toHaveTextContent(/state or evidence changed/i);
  expect(alert).not.toHaveTextContent(/request could not be confirmed/i);
});
it("opens the period's running review when another click finds one already queued", async () => {
  // Staging 2026-10-24: the server refuses a second review of a period whose review is still queued or
  // running, and names it. The page shows that review instead of "could not be loaded or queued".
  const { ApiError } = await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client");
  const normal = vi.mocked(apiClient.get).getMockImplementation()!;
  const queued = { ...run, id: "queued-review", status: "pending" };
  vi.mocked(apiClient.get).mockImplementation(async (path: string) =>
    path.endsWith("/runs/queued-review") ? (queued as never) : normal(path),
  );
  vi.mocked(apiClient.post).mockRejectedValue(
    new ApiError(JSON.stringify({ code: "review_already_running", run_id: "queued-review" }), 409),
  );
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile period" }));
  await waitFor(() =>
    expect(apiClient.get).toHaveBeenCalledWith("/api/v1/transaction-ops/runs/queued-review"),
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "Reconcile period" })).not.toBeDisabled());
  expect(screen.queryByText(/Review data could not be loaded or queued/)).not.toBeInTheDocument();
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

it("explains a group without preparing fixes", async () => {
  mount();
  const link = await screen.findByRole("link", { name: "Explain group →" });
  const prompt = new URL(link.getAttribute("href")!, "https://example.test").searchParams.get("compose")!;
  expect(prompt).toContain("transaction_ops.group_breakdown");
  expect(prompt).not.toContain("transaction_ops.accounting_group");
  expect(prompt).toContain('"review_run_ids":["review-a"]');
});

it("launches one group investigation with all-member pagination and exact scope", async () => {
  mount();
  const link = await screen.findByRole("link", { name: "Prepare group fixes →" });
  const prompt = new URL(
    link.getAttribute("href")!,
    "https://example.test",
  ).searchParams.get("compose")!;
  expect(prompt).toContain("transaction_ops.accounting_group");
  // A mixed group sent straight to the fix prepared nothing: the breakdown runs first.
  expect(prompt.indexOf("transaction_ops.group_breakdown")).toBeGreaterThan(-1);
  expect(prompt.indexOf("transaction_ops.group_breakdown")).toBeLessThan(
    prompt.indexOf("transaction_ops.accounting_group"),
  );
  expect(prompt).toContain("only if a cause's next step is prepare_corrections");
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
    // A string body, not a jsdom Blob: Node's Response needs a web Blob with
    // .stream(), which jsdom's Blob lacks ("object.stream is not a function").
    new Response("workbook", {
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

it("keeps queued reviews distinct from zero results and opens an earlier saved review without running it", async () => {
  const normal = vi.mocked(apiClient.get).getMockImplementation()!;
  const queued = { ...run, id: "queued-review", status: "pending", params_json: { ...run.params_json, review: { id: "new-span", start: "2026-09-07T07:00:00Z", end: "2026-09-14T07:00:00Z" } } };
  vi.mocked(apiClient.get).mockImplementation(async (path: string) => {
    if (path.includes("/runs?")) return [queued, run] as never;
    if (path.includes("queued-review/review")) return { complete: false, status: "running", current_run_status: "pending", completed_slices: 0 } as never;
    if (path.includes("/review-results?") && path.includes("queued-review")) return { summary: { checked: 0, matched: 0, needs_review: 0, not_verified: 0 }, items: [], total: 0, has_next: false } as never;
    return normal(path);
  });
  mount();
  await screen.findByText(/Queued · 0 daily slices complete/);
  expect(screen.getByTestId("stat-checked")).toHaveTextContent("—");
  expect(screen.getByText(/Waiting for this review’s first results/)).toBeVisible();
  const select = screen.getByRole("combobox", { name: "Saved review results" }) as HTMLSelectElement;
  const previous = Array.from(select.options).find(o => o.textContent?.includes("2026-08-31"))!;
  fireEvent.change(select, { target: { value: previous.value } });
  await waitFor(() => expect(screen.getByTestId("stat-checked")).toHaveTextContent("8"));
  expect(await screen.findByText("R123456789")).toBeVisible();
  expect(apiClient.post).not.toHaveBeenCalled();
  expect(apiClient.get).toHaveBeenCalledWith("/api/v1/transaction-ops/runs?limit=200&period_reviews_only=true");
});

it("shows completed daily coverage separately from enabled schedule", async () => {
  const original = vi.mocked(apiClient.get).getMockImplementation()!;
  vi.mocked(apiClient.get).mockImplementation(async (path, ...args) => {
    if (path.endsWith("/daily-status")) return [{
      config_id: "scope-a", status: "behind", checked_through: "2026-09-07",
      last_completed_at: "2026-09-08T18:00:00Z", run_id: "daily-complete",
    }];
    return original(path, ...args);
  });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><TransactionWorkspace /></QueryClientProvider>);
  const link = await screen.findByRole("link", { name: "Checked through 2026-09-07" });
  expect(link).toHaveAttribute("href", "/transaction-operations/runs/daily-complete");
  expect(screen.getByText(/Behind schedule/)).toBeInTheDocument();
  expect(screen.getByText(/Daily checks on/)).toBeInTheDocument();
});

it("preserves review context across the unified views and does not queue work on navigation", async () => {
  const client = new QueryClient({defaultOptions:{queries:{retry:false}}});
  const change = vi.fn();
  const ui = (view: "reconcile" | "cases" | "records" | "history") => <QueryClientProvider client={client}><TransactionWorkspace view={view} onViewChange={change} /></QueryClientProvider>;
  const {rerender} = render(ui("reconcile"));
  await screen.findByText("R123456789");
  fireEvent.change(screen.getByRole("combobox", {name:"Review period"}), {target:{value:"last_month"}});
  rerender(ui("cases"));
  expect(await screen.findByRole("heading", {name:"Open cases"})).toBeVisible();
  expect(screen.getByRole("combobox", {name:"Review period",hidden:true})).not.toBeVisible();
  rerender(ui("records"));
  expect(screen.getByText("Imported source orders")).toBeVisible();
  rerender(ui("reconcile"));
  expect(screen.getByRole("combobox", {name:"Review period"})).toHaveValue("last_month");
  expect(screen.getByText("Imported source orders")).not.toBeVisible();
  rerender(ui("history"));
  fireEvent.click(await screen.findByRole("button", {name:"View period"}));
  expect(change).toHaveBeenCalledWith("reconcile");
  expect(apiClient.post).not.toHaveBeenCalled();
});

it("closes the portaled case drawer on view navigation without submitting a decision", async () => {
  const client = new QueryClient({defaultOptions:{queries:{retry:false}}});
  const ui = (view: "reconcile" | "records") => <QueryClientProvider client={client}><TransactionWorkspace view={view} /></QueryClientProvider>;
  const {rerender} = render(ui("reconcile"));
  fireEvent.click(await screen.findByRole("button", {name:"Review case →"}));
  expect(screen.getByRole("dialog")).toBeVisible();
  rerender(ui("records"));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  rerender(ui("reconcile"));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(apiClient.post).not.toHaveBeenCalled();
});

it("defaults to saved observations and sends that mode on the normal review endpoint", async () => {
  vi.mocked(apiClient.post).mockResolvedValue(run);
  mount();
  expect(screen.getByLabelText("Review evidence")).toHaveValue("saved");
  expect(screen.getByText(/original read dates/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile period" }));
  await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith(
    "/api/v1/transaction-ops/configs/scope-a/review",
    expect.objectContaining({ evidence_mode: "saved", evaluation_key: expect.any(String) }),
  ));
});

it("allows an explicit standard review instead of silently forcing saved observations", async () => {
  vi.mocked(apiClient.post).mockResolvedValue(run);
  mount();
  fireEvent.change(screen.getByLabelText("Review evidence"), { target: { value: "current" } });
  fireEvent.click(screen.getByRole("button", { name: "Reconcile period" }));
  await waitFor(() => expect(apiClient.post).toHaveBeenCalled());
  expect(vi.mocked(apiClient.post).mock.calls[0][1]).not.toHaveProperty("evidence_mode");
});

it("shows a resolved case as reconciled while retaining the raw observed variance", async () => {
  const original = vi.mocked(apiClient.get).getMockImplementation()!;
  vi.mocked(apiClient.get).mockImplementation(async (path: string) => {
    const response = await original(path);
    if (path.includes("/review-results?")) {
      const data = response as {items: Array<Record<string, unknown>>};
      return {...data, items: data.items.map((item) => ({...item, reconciled: true}))} as never;
    }
    return response;
  });
  mount();
  expect(await screen.findByText("Reconciled")).toBeInTheDocument();
  expect(screen.getByText("+123456789012335.123456")).toBeInTheDocument();
});

const remaining = {
  status: "difference",
  currency: "USD",
  amounts: {
    order_total: { source: "87.66", target: "100.00", delta: "-12.34" },
    tax: { source: "6.77", target: "8.00", delta: "-1.23" },
    refunds: { source: "5.00", target: "5.00", delta: "0.00" },
  },
};
function withCase(
  detail: Record<string, unknown> | (() => Record<string, unknown>),
  summary?: Record<string, number>,
  rowFlags: Record<string, unknown> = {},
  runs: Record<string, Record<string, unknown>> = {},
) {
  const original = vi.mocked(apiClient.get).getMockImplementation()!;
  vi.mocked(apiClient.get).mockImplementation(async (path: string) => {
    if (path.endsWith("/cases/case-a"))
      return {
        id: "case-a",
        order_reference: "R123456789",
        scope_json: {},
        last_observed_at: "2026-10-27T17:41:00Z",
        latest_report_json: { balance },
        ...(typeof detail === "function" ? detail() : detail),
      } as never;
    const run = Object.keys(runs).find((id) => path.endsWith(`/runs/${id}`));
    if (run) return { id: run, ...runs[run] } as never;
    if (path.includes("/cases/case-a/observations")) return [] as never;
    const response = await original(path);
    if (path.includes("/review-results?")) {
      const data = response as { items: Array<Record<string, unknown>>; summary: Record<string, number> };
      return { ...data, summary: summary || data.summary, items: data.items.map((item) => ({ ...item, ...rowFlags })) } as never;
    }
    return response;
  });
}

it("counts orders resolved by a person in their own tile and filter, apart from matched", async () => {
  withCase({ status: "resolved" }, { checked: 8, matched: 4, resolved: 1, needs_review: 2, not_verified: 1 }, { resolved: true });
  mount();
  await waitFor(() => expect(screen.getByTestId("stat-resolved")).toHaveTextContent("1"));
  expect(screen.getByTestId("stat-matched")).toHaveTextContent("4");
  expect(await screen.findByText("Resolved by person", { selector: "span" })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Result status"), { target: { value: "resolved" } });
  await waitFor(() =>
    expect(apiClient.get).toHaveBeenCalledWith(expect.stringMatching(/review-results\?.*status=resolved/)),
  );
  // Resolved cases are not in the issue groups, so the groups are not asked for them.
  expect(vi.mocked(apiClient.get).mock.calls.some(([p]) => p.includes("/case-groups?") && p.includes("status=resolved"))).toBe(false);
});

it("accepts a freshly re-checked difference exactly as the server offered it", async () => {
  withCase({
    status: "open",
    resolution_offer: {
      available: true,
      unavailable_reason: null,
      accepted: remaining,
      fresh_until: "2026-10-27T17:56:00Z",
      reasons: ["accepted_residual", "duplicate_or_test_order", "fixed_in_netsuite_rest_in_source", "handled_outside_app"],
    },
  });
  vi.mocked(apiClient.post).mockResolvedValue({ id: "case-a", status: "resolved" });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Review case →" }));
  const accept = await screen.findByRole("button", { name: "Accept as resolved" });
  expect(screen.getByText(/I accept the remaining difference of -12.34 \(order total\) and -1.23 \(tax\) as shown, in USD\./)).toBeInTheDocument();
  expect(accept).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "fixed_in_netsuite_rest_in_source" } });
  expect(accept).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /I accept the remaining difference/ }));
  fireEvent.change(screen.getByLabelText(/Note/), { target: { value: "Solidus ops will void the duplicate." } });
  fireEvent.click(accept);
  await waitFor(() =>
    expect(apiClient.post).toHaveBeenCalledWith("/api/v1/transaction-ops/cases/case-a/resolve", {
      reason: "fixed_in_netsuite_rest_in_source",
      note: "Solidus ops will void the duplicate.",
      accepted: remaining,
    }),
  );
});

it("asks for a re-check before a stale difference can be accepted", async () => {
  withCase({
    status: "open",
    resolution_offer: { available: false, unavailable_reason: "recheck_required", accepted: remaining, fresh_until: "2026-10-26T21:40:00Z", reasons: [] },
  });
  vi.mocked(apiClient.post).mockResolvedValue({ runs: [{ id: "recheck-run", config_id: "scope-a", case_ids: ["case-a"] }], blocked: [] });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Review case →" }));
  expect(await screen.findByText("Fixed this outside the app?")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Accept as resolved" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "I fixed this — re-check" }));
  await waitFor(() =>
    expect(apiClient.post).toHaveBeenCalledWith("/api/v1/transaction-ops/cases/investigate", {
      case_ids: ["case-a"],
      evaluation_key: expect.any(String),
    }),
  );
  expect(await screen.findByText(/Re-checking/)).toBeInTheDocument();
  expect(vi.mocked(apiClient.post).mock.calls.some(([p]) => p.includes("/resolve"))).toBe(false);
});

it("shows who resolved a case, why and what they accepted, and lets a person reopen it", async () => {
  withCase({
    status: "resolved",
    resolution_json: {
      resolved_by: "user-1",
      resolved_by_name: "T. Accountant",
      resolved_at: "2026-10-27T17:43:00Z",
      reason: "fixed_in_netsuite_rest_in_source",
      note: "Solidus ops will void the duplicate.",
      accepted: remaining,
      observed_at: "2026-10-27T17:41:00Z",
    },
  });
  vi.mocked(apiClient.post).mockResolvedValue({ id: "case-a", status: "open" });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Review case →" }));
  expect(await screen.findByText(/Resolved by T. Accountant/)).toBeInTheDocument();
  expect(screen.getByText(/Fixed in NetSuite; the remaining difference is in Solidus/)).toBeInTheDocument();
  expect(screen.getByText(/order total -12.34, tax -1.23, refunds 0.00/)).toBeInTheDocument();
  expect(screen.getByText(/Solidus ops will void the duplicate/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
  await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith("/api/v1/transaction-ops/cases/case-a/reopen", {}));
});

const offer = (accepted: Record<string, unknown>) => ({
  status: "open",
  resolution_offer: {
    available: true,
    unavailable_reason: null,
    accepted,
    fresh_until: "2026-10-27T17:56:00Z",
    reasons: ["fixed_in_netsuite_rest_in_source"],
  },
});
const stale = {
  status: "open",
  resolution_offer: { available: false, unavailable_reason: "recheck_required", accepted: remaining, fresh_until: null, reasons: [] },
};

it("drops the confirmation when the offered difference changes before Accept", async () => {
  // Codex round 2 F1: a confirmation is for the amounts the person saw, not whatever the next poll brings.
  let current: Record<string, unknown> = remaining;
  withCase(() => offer(current));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <TransactionWorkspace />
    </QueryClientProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Review case →" }));
  const accept = await screen.findByRole("button", { name: "Accept as resolved" });
  fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "fixed_in_netsuite_rest_in_source" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /I accept the remaining difference/ }));
  expect(accept).toBeEnabled();
  current = { ...remaining, amounts: { ...remaining.amounts, tax: { source: "5.00", target: "8.00", delta: "-3.00" } } };
  await act(() => client.invalidateQueries());
  await waitFor(() => expect(screen.getByText(/-3.00 \(tax\) as shown/)).toBeInTheDocument());
  expect(screen.getByRole("checkbox", { name: /I accept the remaining difference/ })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Accept as resolved" })).toBeDisabled();
  expect(apiClient.post).not.toHaveBeenCalled();
});

it("says why a re-check could not start instead of waiting forever", async () => {
  // Codex round 2 F3: a blocked re-check queues no run.
  withCase(stale);
  vi.mocked(apiClient.post).mockResolvedValue({ runs: [], blocked: [{ case_id: "case-a", code: "case_scope_unavailable" }] });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Review case →" }));
  fireEvent.click(await screen.findByRole("button", { name: "I fixed this — re-check" }));
  expect(await screen.findByText(/could not start/)).toBeInTheDocument();
  expect(screen.queryByText("Re-checking")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "I fixed this — re-check" })).toBeEnabled();
});

it("ends the re-check when its run finishes without a new reading", async () => {
  // Codex round 2 F3: an investigation can stop (error, budget) without observing the order again.
  withCase(stale, undefined, {}, { "recheck-run": { status: "finished", termination_reason: "error" } });
  vi.mocked(apiClient.post).mockResolvedValue({ runs: [{ id: "recheck-run", config_id: "scope-a", case_ids: ["case-a"] }], blocked: [] });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Review case →" }));
  fireEvent.click(await screen.findByRole("button", { name: "I fixed this — re-check" }));
  expect(await screen.findByText(/finished without a new reading/)).toBeInTheDocument();
  expect(screen.getByText(/Investigation error/)).toBeInTheDocument();
  expect(screen.queryByText("Re-checking")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "I fixed this — re-check" })).toBeEnabled();
});
