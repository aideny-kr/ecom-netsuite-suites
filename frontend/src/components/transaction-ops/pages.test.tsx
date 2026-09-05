import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  act,
} from "@testing-library/react";
import { TransactionOperationsPage } from "./operations-page";
import { TransactionRunPage } from "./run-page";
const mocks = vi.hoisted(() => ({
  allowed: true,
  tenant: "tenant",
  configs: [] as object[],
  runs: [] as object[],
  start: vi.fn(),
  push: vi.fn(),
  run: null as object | null,
  findings: { items: [] as object[], hasNext: false },
  proposals: { items: [] as object[], hasNext: false },
  findingsError: null as Error | null,
  findingsHook: vi.fn(),
  proposalsHook: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));
vi.mock("@/hooks/use-transaction-ops", () => ({
  useTransactionAccess: () => ({
    allowed: mocks.allowed,
    loading: false,
    error: null,
    tenantId: mocks.tenant,
  }),
  useTransactionConfigs: () => ({
    data: mocks.configs,
    isLoading: false,
    error: null,
  }),
  useTransactionRuns: () => ({
    data: mocks.runs,
    isLoading: false,
    error: null,
  }),
  useStartTransactionRun: () => ({
    mutateAsync: mocks.start,
    isPending: false,
  }),
  useTransactionRun: () => ({ data: mocks.run, isLoading: false, error: null }),
  useTransactionFindings: (...args: unknown[]) => {
    mocks.findingsHook(...args);
    return {
      data: mocks.findings,
      isLoading: false,
      error: mocks.findingsError,
    };
  },
  useTransactionProposals: (...args: unknown[]) => {
    mocks.proposalsHook(...args);
    return { data: mocks.proposals, isLoading: false, error: null };
  },
}));
const config = {
  id: "config",
  name: "EU sales orders",
  netsuite_account_id: "EXAMPLE_SB1",
  subsidiary_id: "4",
  record_type: "salesorder",
  enabled: true,
  schedule_enabled: false,
  max_api_calls: 100,
  max_orders: 20,
  deadline_seconds: 600,
};
beforeEach(() => {
  vi.clearAllMocks();
  mocks.allowed = true;
  mocks.tenant = "tenant";
  mocks.configs = [];
  mocks.runs = [];
  mocks.run = null;
  mocks.findings = { items: [], hasNext: false };
  mocks.proposals = { items: [], hasNext: false };
  mocks.findingsError = null;
});
describe("transaction operations pages", () => {
  it("has an honest administrator setup empty state and Connections link", () => {
    render(<TransactionOperationsPage />);
    expect(screen.getByText(/administrator.*configure/i)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open Connections" }),
    ).toHaveAttribute("href", "/connections");
    expect(
      screen.queryByRole("button", { name: "Start investigation" }),
    ).not.toBeInTheDocument();
  });
  it("does not display forms or records without both features and reconciliation permission", () => {
    mocks.allowed = false;
    mocks.configs = [config];
    render(<TransactionOperationsPage />);
    expect(screen.getByText(/reconciliation access/i)).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Full order references"),
    ).not.toBeInTheDocument();
  });
  it("queues durable run and navigates only after server returns, preserving retry identity", async () => {
    mocks.configs = [config];
    mocks.start
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce({ id: "durable-run" });
    render(<TransactionOperationsPage />);
    fireEvent.change(screen.getByLabelText("Full order references"), {
      target: { value: "R123456789-EU" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Start investigation" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "could not be confirmed",
      ),
    );
    expect(mocks.push).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: "Start investigation" }),
    );
    await waitFor(() =>
      expect(mocks.push).toHaveBeenCalledWith(
        "/transaction-operations/runs/durable-run",
      ),
    );
    expect(mocks.start.mock.calls[0][0]).toEqual(mocks.start.mock.calls[1][0]);
    expect(mocks.start.mock.calls[0][0].order_references).toEqual([
      "R123456789-EU",
    ]);
  });
  it("shows partial budget termination and paginates findings instead of claiming all matched", () => {
    mocks.run = {
      id: "run",
      config_id: "config",
      status: "finished",
      termination_reason: "budget",
      origin: "manual",
      config_snapshot: config,
      params_json: { order_references: ["R123456789-EU"] },
      progress_json: { processed: 2, matched: 1, needs_review: 1 },
      orders_used: 3,
      max_orders: 20,
      api_calls_used: 100,
      max_api_calls: 100,
      deadline_at: "2026-09-04T15:00:00Z",
      created_at: "2026-09-04T14:00:00Z",
      finished_at: null,
    };
    mocks.findings.hasNext = true;
    render(<TransactionRunPage id="run" />);
    expect(screen.getByText("Stopped at budget")).toBeInTheDocument();
    expect(
      screen.getByText(/does not mean every order was examined/i),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next findings page" }));
    expect(mocks.findingsHook).toHaveBeenLastCalledWith("run", 100);
    expect(screen.getByText(/No proposals recorded/)).toBeInTheDocument();
  });
  it("surfaces finding load failures instead of an empty clean report", () => {
    mocks.run = {
      id: "run",
      status: "finished",
      termination_reason: "done",
      config_snapshot: {},
      params_json: {},
      progress_json: {},
      max_api_calls: 1,
      max_orders: 1,
      orders_used: 1,
      api_calls_used: 1,
    };
    mocks.findingsError = new Error("network");
    render(<TransactionRunPage id="run" />);
    expect(
      screen.getByText(/Findings could not be loaded/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("No findings recorded yet."),
    ).not.toBeInTheDocument();
  });
});

it("does not navigate to a previous workspace after an in-flight run creation unmounts", async () => {
  let resolve!: (run: { id: string }) => void;
  mocks.configs = [config];
  mocks.start.mockImplementationOnce(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const { unmount } = render(<TransactionOperationsPage />);
  fireEvent.change(screen.getByLabelText("Full order references"), {
    target: { value: "R123456789-EU" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Start investigation" }));
  await waitFor(() => expect(mocks.start).toHaveBeenCalledTimes(1));
  unmount();
  await act(async () => resolve({ id: "previous-tenant-run" }));
  expect(mocks.push).not.toHaveBeenCalled();
});

it("does not navigate when the workspace changes before run creation resolves", async () => {
  let resolve!: (run: { id: string }) => void;
  mocks.configs = [config];
  mocks.start.mockImplementationOnce(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const { rerender } = render(<TransactionOperationsPage />);
  fireEvent.change(screen.getByLabelText("Full order references"), {
    target: { value: "R123456789-EU" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Start investigation" }));
  await waitFor(() => expect(mocks.start).toHaveBeenCalledTimes(1));
  mocks.tenant = "new-tenant";
  rerender(<TransactionOperationsPage />);
  await act(async () => resolve({ id: "previous-tenant-run" }));
  expect(mocks.push).not.toHaveBeenCalled();
});
