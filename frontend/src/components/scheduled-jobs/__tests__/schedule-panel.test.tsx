import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

// Scheduled Jobs platform, Task 6 (spec §B6, mock state two — "Schedule").

const mocks = vi.hoisted(() => ({ update: vi.fn() }));
vi.mock("@/hooks/use-scheduled-jobs", () => ({ useUpdateSchedule: () => mocks.update() }));

import { SchedulePanel } from "@/components/scheduled-jobs/schedule-panel";
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
    timezone: "America/Los_Angeles",
    delivery_json: null,
    budget_json: { bytes_scanned: 5_000_000_000, seconds: 600, usd: 2 },
    catch_up: "once",
    last_run_at: null,
    last_run_status: null,
    next_run_at: "2026-09-14T13:00:00Z",
    paused_at: null,
    pause_reason: null,
    kinds: ["read", "write"],
    summary_line: null,
    has_pending_plan: false,
    plan_json: null,
    pending_plan_json: null,
    pending_plan_reason: null,
    pending_plan_diff: [],
    owner_id: null,
    ...overrides,
  };
}

const NOW = new Date("2026-09-09T00:00:00Z");

let mutate: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.setSystemTime(NOW);
  mutate = vi.fn();
  mocks.update.mockReturnValue({ mutate, isPending: false });
});

it("highlights Weekly in the segmented control for a weekly cron, and shows the timezone + next run", () => {
  wrap(<SchedulePanel schedule={detail()} />);
  const weekly = screen.getByText("Weekly");
  expect(weekly.className).toMatch(/on|active|accent/); // visually marked "on" somehow
  expect(screen.getByText("America/Los_Angeles")).toBeInTheDocument();
  expect(screen.getByText(/in 5 d/)).toBeInTheDocument();
});

it("shows the catch-up policy and the budget line", () => {
  wrap(<SchedulePanel schedule={detail()} />);
  expect(screen.getByText(/run once when back, never twice/)).toBeInTheDocument();
  expect(screen.getByText(/5 GB.*10 min.*\$2/)).toBeInTheDocument();
});

it("Edit opens editable cadence/weekday/time/timezone fields, and Save PATCHes cron_expression + timezone", () => {
  wrap(<SchedulePanel schedule={detail()} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));

  const tzInput = screen.getByLabelText(/time zone/i) as HTMLInputElement;
  fireEvent.change(tzInput, { target: { value: "America/New_York" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  const [body] = mutate.mock.calls[0];
  expect(body.timezone).toBe("America/New_York");
  expect(body.cron_expression).toBe("0 6 * * 1");
});
