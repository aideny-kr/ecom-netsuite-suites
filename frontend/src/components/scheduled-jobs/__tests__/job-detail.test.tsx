import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

// Scheduled Jobs platform, Task 6 (spec §B6, mock state two). Integration-
// style: renders the real child panels, mocking the whole hooks module
// (jobs-list.test.tsx's established pattern) since this level is about
// composition + head actions, not any one panel's own internals (each
// already covered by its own test file).

const mocks = vi.hoisted(() => ({
  scheduledJob: vi.fn(),
  update: vi.fn(),
  approve: vi.fn(),
  run: vi.fn(),
  pause: vi.fn(),
  resume: vi.fn(),
  del: vi.fn(),
  runs: vi.fn(),
}));

vi.mock("@/hooks/use-scheduled-jobs", () => ({
  useScheduledJob: () => mocks.scheduledJob(),
  useUpdateSchedule: () => mocks.update(),
  useApproveSchedule: () => mocks.approve(),
  useRunSchedule: () => mocks.run(),
  usePauseSchedule: () => mocks.pause(),
  useResumeScheduledJob: () => mocks.resume(),
  useDeleteSchedule: () => mocks.del(),
  useScheduleRuns: () => mocks.runs(),
}));

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: routerPush }) }));

import { JobDetail } from "@/components/scheduled-jobs/job-detail";

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
    timezone: "America/Los_Angeles",
    delivery_json: { drive: { folder: "Reports / Inventory aging" } },
    budget_json: null,
    catch_up: "once",
    last_run_at: "2026-09-08T13:00:00Z",
    last_run_status: "done",
    next_run_at: "2026-09-14T13:00:00Z",
    paused_at: null,
    pause_reason: null,
    kinds: ["read", "write"],
    summary_line: null,
    has_pending_plan: false,
    plan_json: { steps: [{ id: "q1", type: "bigquery_sql", params: { query: "SELECT 1" } }] },
    pending_plan_json: null,
    pending_plan_reason: null,
    pending_plan_diff: [],
    owner_id: null,
    ...overrides,
  };
}

let runMutate: ReturnType<typeof vi.fn>;
let pauseMutate: ReturnType<typeof vi.fn>;
let resumeMutate: ReturnType<typeof vi.fn>;
let deleteMutate: ReturnType<typeof vi.fn>;

beforeEach(() => {
  routerPush.mockClear();
  runMutate = vi.fn();
  pauseMutate = vi.fn();
  resumeMutate = vi.fn();
  deleteMutate = vi.fn();

  mocks.scheduledJob.mockReturnValue({ data: detail(), isPending: false, isError: false, refetch: vi.fn() });
  mocks.update.mockReturnValue({ mutate: vi.fn(), isPending: false });
  mocks.approve.mockReturnValue({ mutate: vi.fn(), isPending: false });
  mocks.run.mockReturnValue({ mutate: runMutate, isPending: false });
  mocks.pause.mockReturnValue({ mutate: pauseMutate, isPending: false });
  mocks.resume.mockReturnValue({ mutate: resumeMutate, isPending: false });
  mocks.del.mockReturnValue({ mutate: deleteMutate, isPending: false, error: null });
  mocks.runs.mockReturnValue({ data: [], isPending: false, isError: false, refetch: vi.fn() });
});

it("renders the title and an 'active' pill for an approved, unpaused schedule", () => {
  wrap(<JobDetail id="s-1" />);
  expect(screen.getByRole("heading", { name: "Inventory Aging Weekly" })).toBeInTheDocument();
  expect(screen.getByText("active")).toBeInTheDocument();
});

it("Run now calls run(use_pending=false) when the plan is approved", () => {
  wrap(<JobDetail id="s-1" />);
  fireEvent.click(screen.getByRole("button", { name: "Run now" }));
  expect(runMutate).toHaveBeenCalledWith(false);
});

it("Run now is disabled when the plan isn't approved", () => {
  mocks.scheduledJob.mockReturnValue({
    data: detail({ plan_status: "pending_approval" }),
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  wrap(<JobDetail id="s-1" />);
  expect(screen.getByRole("button", { name: "Run now" })).toBeDisabled();
});

it("Pause calls the pause mutation on an active schedule", () => {
  wrap(<JobDetail id="s-1" />);
  fireEvent.click(screen.getByRole("button", { name: "Pause" }));
  expect(pauseMutate).toHaveBeenCalled();
});

it("shows Resume instead of Pause, and a 'paused' pill, once paused_at is set", () => {
  mocks.scheduledJob.mockReturnValue({
    data: detail({ paused_at: "2026-09-05T18:00:00Z", pause_reason: "NetSuite token expired" }),
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  wrap(<JobDetail id="s-1" />);
  expect(screen.getByText("paused")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Resume" }));
  expect(resumeMutate).toHaveBeenCalledWith("s-1");
});

it("Delete opens a confirm dialog; confirming deletes and navigates back to the list", async () => {
  wrap(<JobDetail id="s-1" />);
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete job" }));

  expect(deleteMutate).toHaveBeenCalled();
  const [, opts] = deleteMutate.mock.calls[0] ?? [undefined, undefined];
  opts?.onSuccess?.();
  expect(routerPush).toHaveBeenCalledWith("/scheduled-jobs");
});
