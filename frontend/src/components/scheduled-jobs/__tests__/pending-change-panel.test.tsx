import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

// Scheduled Jobs platform, Task 6 (spec §B6, mock state two — "Pending
// change"). Renders only when `pending_plan_json` is present; the three
// buttons are wired to their own mutation hooks (mocked here, per
// jobs-list.test.tsx's established pattern).

const mocks = vi.hoisted(() => ({ approve: vi.fn(), run: vi.fn(), update: vi.fn() }));

vi.mock("@/hooks/use-scheduled-jobs", () => ({
  useApproveSchedule: () => mocks.approve(),
  useRunSchedule: () => mocks.run(),
  useUpdateSchedule: () => mocks.update(),
}));

import { PendingChangePanel } from "@/components/scheduled-jobs/pending-change-panel";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function detail(overrides: Partial<ScheduleDetail> = {}): ScheduleDetail {
  return {
    id: "s-1",
    tenant_id: "t-1",
    name: "Inventory Aging Weekly",
    schedule_type: "job",
    cron_expression: "0 6 * * 1",
    is_active: true,
    parameters: null,
    instruction: "weekly inventory aging",
    plan_status: "approved",
    plan_version: 3,
    timezone: "UTC",
    delivery_json: null,
    budget_json: null,
    catch_up: "once",
    last_run_at: null,
    last_run_status: null,
    next_run_at: null,
    paused_at: null,
    pause_reason: null,
    kinds: ["read", "write"],
    summary_line: null,
    has_pending_plan: true,
    plan_json: { steps: [] },
    pending_plan_json: { steps: [{ id: "q1", type: "bigquery_sql", params: {} }] },
    pending_plan_reason: "instruction edited",
    pending_plan_diff: [
      { kind: "ctx", step: 1, text: "step 1 · bigquery_sql · location filter" },
      { kind: "del", step: 1, text: "-  WHERE location IN ('Dimerco','Fedex','Panurgy')" },
      { kind: "add", step: 1, text: "+  WHERE location IN ('Dimerco','Fedex','Panurgy','Virtual')" },
    ],
    owner_id: null,
    ...overrides,
  };
}

let approveMutate: ReturnType<typeof vi.fn>;
let runMutate: ReturnType<typeof vi.fn>;
let updateMutate: ReturnType<typeof vi.fn>;

beforeEach(() => {
  approveMutate = vi.fn();
  runMutate = vi.fn();
  updateMutate = vi.fn();
  mocks.approve.mockReturnValue({ mutate: approveMutate, isPending: false });
  mocks.run.mockReturnValue({ mutate: runMutate, isPending: false });
  mocks.update.mockReturnValue({ mutate: updateMutate, isPending: false });
});

it("renders nothing when there is no pending plan", () => {
  const { container } = wrap(<PendingChangePanel schedule={detail({ pending_plan_json: null })} />);
  expect(container).toBeEmptyDOMElement();
});

it("renders the diff lines with add/del/ctx distinguished", () => {
  wrap(<PendingChangePanel schedule={detail()} />);
  expect(screen.getByText(/location filter/)).toBeInTheDocument();
  expect(screen.getByText(/- WHERE location IN.*Fedex.*Panurgy'\)/)).toBeInTheDocument();
  expect(screen.getByText(/\+ WHERE location IN.*Virtual/)).toBeInTheDocument();
});

it("Approve calls the approve mutation", () => {
  wrap(<PendingChangePanel schedule={detail()} />);
  fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
  expect(approveMutate).toHaveBeenCalled();
});

it("Run once with this change calls run(use_pending=true)", () => {
  wrap(<PendingChangePanel schedule={detail()} />);
  fireEvent.click(screen.getByRole("button", { name: "Run once with this change" }));
  expect(runMutate).toHaveBeenCalledWith(true);
});

it("Discard PATCHes discard_pending: true", () => {
  wrap(<PendingChangePanel schedule={detail()} />);
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(updateMutate).toHaveBeenCalledWith({ discard_pending: true });
});
