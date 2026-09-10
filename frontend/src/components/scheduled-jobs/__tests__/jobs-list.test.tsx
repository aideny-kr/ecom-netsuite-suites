import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ScheduledJob, ScheduledJobsListResponse } from "@/hooks/use-scheduled-jobs";

// Scheduled Jobs platform, Task 5 (spec §B6, mock state one) — the list page.
// Mocks every hook module directly (celigo-integrations-page.test.tsx's
// established pattern) rather than the transport layer, since this
// component's own logic (tiles, cell rendering, action gating) is what's
// under test, not the hooks it calls.

const mocks = vi.hoisted(() => ({
  scheduledJobs: vi.fn(),
  jobSchedules: vi.fn(),
  planInfo: vi.fn(),
  runNow: vi.fn(),
  resume: vi.fn(),
}));

vi.mock("@/hooks/use-scheduled-jobs", () => ({
  useScheduledJobs: () => mocks.scheduledJobs(),
  useRunScheduleNow: () => mocks.runNow(),
  useResumeScheduledJob: () => mocks.resume(),
}));

vi.mock("@/hooks/use-jobs", () => ({
  useJobSchedules: () => mocks.jobSchedules(),
}));

vi.mock("@/hooks/use-plan", () => ({
  usePlanInfo: () => mocks.planInfo(),
}));

import { ScheduledJobsList } from "@/components/scheduled-jobs/jobs-list";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function job(overrides: Partial<ScheduledJob> = {}): ScheduledJob {
  return {
    id: "s-1",
    tenant_id: "t-1",
    name: "Inventory Aging Weekly",
    schedule_type: "job",
    cron_expression: "0 6 * * 1",
    is_active: true,
    parameters: null,
    instruction: "Every Monday at 6am Pacific, build the inventory aging report…",
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
    summary_line: "5 steps · Query the inventory snapshot → Compose the report → Render → Upload → Finish",
    has_pending_plan: false,
    last_run_duration_seconds: 112,
    runs_last_7_days: 1,
    owner_name: "Aiden Yi",
    created_via: "chat",
    ...overrides,
  };
}

/** `GET /api/v1/schedules`'s response shape (Task 5 residual): the tenant's
 * rows plus the page's tenant-wide "Last 7 days" tile totals — see
 * `ScheduledJobsListResponse`'s own docstring for why the tile isn't
 * derived from the rows client-side. */
function listData(
  schedules: ScheduledJob[],
  overrides: Partial<Pick<ScheduledJobsListResponse, "runs_last_7_days_total" | "runs_last_7_days_failed">> = {},
): ScheduledJobsListResponse {
  return { schedules, runs_last_7_days_total: 0, runs_last_7_days_failed: 0, ...overrides };
}

const NOW = new Date("2026-09-09T00:00:00Z");

beforeEach(() => {
  vi.setSystemTime(NOW);
  mocks.scheduledJobs.mockReturnValue({
    data: listData([job()], { runs_last_7_days_total: 1, runs_last_7_days_failed: 0 }),
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  mocks.jobSchedules.mockReturnValue({
    data: [{ name: "report-auto-refresh-sweep", task: "tasks.report_auto_refresh_all", schedule: "3600.0", enabled: true }],
    isPending: false,
    isError: false,
  });
  mocks.planInfo.mockReturnValue({
    data: { plan: "pro", limits: { max_schedules: 5 }, usage: { schedules: 2 } },
    isPending: false,
    isError: false,
  });
  mocks.runNow.mockReturnValue({ mutate: vi.fn(), isPending: false });
  mocks.resume.mockReturnValue({ mutate: vi.fn(), isPending: false });
});

// --- Tiles -----------------------------------------------------------------

it("renders the four tiles with real counts from the list + plan usage", () => {
  wrap(<ScheduledJobsList />);
  expect(screen.getByText("Jobs")).toBeInTheDocument();
  expect(screen.getByText("Last 7 days")).toBeInTheDocument();
  expect(screen.getByText("Needs attention")).toBeInTheDocument();
  expect(screen.getByText("Next run")).toBeInTheDocument();
  // 1 job row + 1 system row = 2 total, "1 yours, 1 system"
  expect(screen.getByText(/1 yours, 1 system/)).toBeInTheDocument();
  // usage.schedules of limits.max_schedules
  expect(screen.getByText(/2 of 5 in your plan's quota/)).toBeInTheDocument();
});

it("renders the Last 7 days tile from the tenant-wide aggregate, not a client-side sum of the rows", () => {
  // Two schedules, several runs (Task 5 residual: "the aggregate query").
  // The totals below deliberately DON'T match what summing the two rows'
  // own `runs_last_7_days` would give (1 + 1 = 2) — proving the tile reads
  // the server's own tenant-wide total/failed fields, not a client re-derive.
  mocks.scheduledJobs.mockReturnValue({
    data: listData(
      [job(), job({ id: "s-2", name: "Stripe payout reconciliation" })],
      { runs_last_7_days_total: 21, runs_last_7_days_failed: 1 },
    ),
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  wrap(<ScheduledJobsList />);
  const tile = screen.getByText("Last 7 days").closest("div")!;
  expect(within(tile).getByText("21 runs")).toBeInTheDocument();
  expect(within(tile).getByText("20 done · 1 failed")).toBeInTheDocument();
});

it("counts a pending-approval or paused schedule as needing attention", () => {
  mocks.scheduledJobs.mockReturnValue({
    data: listData([job(), job({ id: "s-2", name: "Stripe payout reconciliation", plan_status: "pending_approval" })]),
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  wrap(<ScheduledJobsList />);
  const tile = screen.getByText("Needs attention").closest("div")!;
  expect(within(tile).getByText("1")).toBeInTheDocument();
});

it("counts an approved schedule with a pending recompiled plan change as needing attention", () => {
  // The mock's own scenario for this tile (state one: "a plan change awaiting
  // approval") is an APPROVED schedule whose instruction was edited since —
  // plan_status stays "approved"; only has_pending_plan flips.
  mocks.scheduledJobs.mockReturnValue({
    data: listData([
      job({ id: "s-2", name: "Inventory Aging Weekly (edited)", plan_status: "approved", has_pending_plan: true }),
    ]),
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  wrap(<ScheduledJobsList />);
  const tile = screen.getByText("Needs attention").closest("div")!;
  expect(within(tile).getByText("1")).toBeInTheDocument();
});

// --- Table cells -------------------------------------------------------------

it("renders READ/WRITE tags and the plan summary in the Does column", () => {
  wrap(<ScheduledJobsList />);
  expect(screen.getByText("READ")).toBeInTheDocument();
  expect(screen.getByText("WRITE")).toBeInTheDocument();
  expect(screen.getByText(/Query the inventory snapshot/)).toBeInTheDocument();
});

it("renders the human schedule label with the raw cron as a sub-line", () => {
  wrap(<ScheduledJobsList />);
  expect(screen.getByText("Weekly · Mon 06:00")).toBeInTheDocument();
  expect(screen.getByText("cron 0 6 * * 1")).toBeInTheDocument();
});

it("renders the last-run pill and when for a completed run", () => {
  wrap(<ScheduledJobsList />);
  expect(screen.getByText("done")).toBeInTheDocument();
  // The Last run and Next columns both carry a "Sep" date — assert at least
  // one, rather than pinning to a single ambiguous match.
  expect(screen.getAllByText(/Sep/).length).toBeGreaterThan(0);
});

it("renders the Last run cell's duration when present (Task 5 residual)", () => {
  wrap(<ScheduledJobsList />);
  const row = screen.getByText("Inventory Aging Weekly").closest("tr")!;
  expect(within(row).getByText(/1m 52s/)).toBeInTheDocument();
});

it("omits the duration from the Last run cell when there is none yet", () => {
  mocks.scheduledJobs.mockReturnValue({
    data: listData([job({ last_run_duration_seconds: null, last_run_at: null, last_run_status: null })]),
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  wrap(<ScheduledJobsList />);
  const row = screen.getByText("Inventory Aging Weekly").closest("tr")!;
  expect(within(row).queryByText(/\dm \ds/)).toBeNull();
});

it("renders the Job column sub-line as 'from the chat · owner {name}' for a chat-created schedule", () => {
  wrap(<ScheduledJobsList />);
  const row = screen.getByText("Inventory Aging Weekly").closest("tr")!;
  expect(within(row).getByText("from the chat · owner Aiden Yi")).toBeInTheDocument();
});

it("renders the Job column sub-line as just 'owner {name}' for a page-created schedule", () => {
  mocks.scheduledJobs.mockReturnValue({
    data: listData([job({ created_via: "page" })]),
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  wrap(<ScheduledJobsList />);
  const row = screen.getByText("Inventory Aging Weekly").closest("tr")!;
  expect(within(row).getByText("owner Aiden Yi")).toBeInTheDocument();
});

it("marks a row with a pending recompiled plan change so it isn't silently invisible", () => {
  mocks.scheduledJobs.mockReturnValue({
    data: listData([job({ has_pending_plan: true })]),
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  wrap(<ScheduledJobsList />);
  const row = screen.getByText("Inventory Aging Weekly").closest("tr")!;
  expect(within(row).getByText("pending change")).toBeInTheDocument();
});

it("does not mark a row with no pending plan change", () => {
  wrap(<ScheduledJobsList />);
  const row = screen.getByText("Inventory Aging Weekly").closest("tr")!;
  expect(within(row).queryByText("pending change")).toBeNull();
});

it("renders the Delivers to column from delivery_json", () => {
  wrap(<ScheduledJobsList />);
  expect(screen.getByText("Drive")).toBeInTheDocument();
  expect(screen.getByText("Reports / Inventory aging")).toBeInTheDocument();
});

// --- Actions -----------------------------------------------------------------

it("shows Run now for an approved, unpaused job and calls the run mutation", () => {
  const mutate = vi.fn();
  mocks.runNow.mockReturnValue({ mutate, isPending: false });
  wrap(<ScheduledJobsList />);
  fireEvent.click(screen.getByRole("button", { name: "Run now" }));
  expect(mutate).toHaveBeenCalledWith("s-1");
});

it("shows Resume (not Run now) for a paused job and calls the resume mutation", () => {
  mocks.scheduledJobs.mockReturnValue({
    data: listData([
      job({
        id: "s-2",
        name: "Stripe payout reconciliation",
        cron_expression: "0 18 * * 5",
        last_run_status: "paused",
        paused_at: "2026-09-05T18:02:00Z",
        pause_reason: "paused after 2 failed attempts: NetSuite token expired",
      }),
    ]),
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  const mutate = vi.fn();
  mocks.resume.mockReturnValue({ mutate, isPending: false });
  wrap(<ScheduledJobsList />);
  expect(screen.queryByRole("button", { name: "Run now" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Resume" }));
  expect(mutate).toHaveBeenCalledWith("s-2");
});

// --- System rows ---------------------------------------------------------------

it("renders system rows with the system pill and Open, never Run now/Resume", () => {
  wrap(<ScheduledJobsList />);
  const row = screen.getByText("report-auto-refresh-sweep").closest("tr")!;
  expect(within(row).getByText("system")).toBeInTheDocument();
  expect(within(row).getByText("Open")).toBeInTheDocument();
  expect(within(row).queryByRole("button", { name: "Run now" })).toBeNull();
  expect(within(row).queryByRole("button", { name: "Resume" })).toBeNull();
});

// --- Query states --------------------------------------------------------------

it("never renders a fabricated 0 while the schedules query is pending", () => {
  mocks.scheduledJobs.mockReturnValue({ data: undefined, isPending: true, isError: false, refetch: vi.fn() });
  wrap(<ScheduledJobsList />);
  expect(screen.queryByText("0")).toBeNull();
  expect(screen.queryByText(/0 yours/)).toBeNull();
});

it("renders an error notice with retry when the schedules query fails", () => {
  const refetch = vi.fn();
  mocks.scheduledJobs.mockReturnValue({ data: undefined, isPending: false, isError: true, refetch });
  wrap(<ScheduledJobsList />);
  expect(screen.getByText(/couldn.t load/i)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /retry/i }));
  expect(refetch).toHaveBeenCalled();
});

it("shows the exact empty-state copy when the tenant has no scheduled jobs", () => {
  mocks.scheduledJobs.mockReturnValue({ data: listData([]), isPending: false, isError: false, refetch: vi.fn() });
  mocks.jobSchedules.mockReturnValue({ data: [], isPending: false, isError: false });
  wrap(<ScheduledJobsList />);
  expect(
    screen.getByText("No scheduled jobs yet. Describe one in plain language, or ask the chat to schedule something."),
  ).toBeInTheDocument();
});

// --- Static copy -----------------------------------------------------------------

it("shows the footer hint copy verbatim", () => {
  wrap(<ScheduledJobsList />);
  expect(
    screen.getByText(
      "System jobs are the platform's own schedules (sync, refresh, health). They are shown so nothing runs invisibly, but only their history is yours to read. A job is never deleted by a run; pausing keeps its history and its last outputs.",
    ),
  ).toBeInTheDocument();
});
