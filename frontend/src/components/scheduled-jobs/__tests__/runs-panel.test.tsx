import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ScheduleRun } from "@/hooks/use-scheduled-jobs";

// Scheduled Jobs platform, Task 6 (spec §B6, mock state two — "Runs").

const mocks = vi.hoisted(() => ({ runs: vi.fn() }));
vi.mock("@/hooks/use-scheduled-jobs", () => ({ useScheduleRuns: () => mocks.runs() }));

import { RunsPanel } from "@/components/scheduled-jobs/runs-panel";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function run(overrides: Partial<ScheduleRun> = {}): ScheduleRun {
  return {
    id: "j-1",
    status: "completed",
    reason: "done",
    started_at: "2026-09-08T06:00:00Z",
    completed_at: "2026-09-08T06:01:52Z",
    correlation_id: "corr-1",
    plan_version: 3,
    attempt: 1,
    outputs: { pdf: { url: "x" }, xlsx: { url: "y" }, report_version: 3 },
    detail: null,
    ...overrides,
  };
}

beforeEach(() => {
  mocks.runs.mockReturnValue({ data: [run()], isPending: false, isError: false, refetch: vi.fn() });
});

it("shows the run's when/took/ended-reason columns", () => {
  wrap(<RunsPanel scheduleId="s-1" />);
  expect(screen.getByText("1m 52s")).toBeInTheDocument();
  expect(screen.getByText("done")).toBeInTheDocument();
});

it("shows the failure detail text alongside a non-done reason", () => {
  mocks.runs.mockReturnValue({
    data: [run({ reason: "error", detail: "drive: folder not found", status: "failed" })],
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  });
  wrap(<RunsPanel scheduleId="s-1" />);
  expect(screen.getByText(/drive: folder not found/)).toBeInTheDocument();
});

it("shows an empty state when there are no runs yet", () => {
  mocks.runs.mockReturnValue({ data: [], isPending: false, isError: false, refetch: vi.fn() });
  wrap(<RunsPanel scheduleId="s-1" />);
  expect(screen.getByText(/No runs yet/)).toBeInTheDocument();
});
