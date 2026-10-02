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

it("does not infer verification from done and links the concrete report output", () => {
  mocks.runs.mockReturnValue({ data: [run({ outputs: { compose: { report_id: "11111111-1111-4111-8111-111111111111", version: 2 } } })], isPending: false });
  wrap(<RunsPanel scheduleId="s-1" />);
  expect(screen.getByText("Not verified")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Report/ })).toHaveAttribute("href", "/reports/11111111-1111-4111-8111-111111111111");
  expect(screen.getByText(/corr-1/)).toBeInTheDocument();
});

it("never turns provider output text into an unsafe external link", () => {
  mocks.runs.mockReturnValue({ data: [run({ verification: "uncertain", outputs: { upload: { pdf_url: "javascript:alert(1)" } } })], isPending: false });
  wrap(<RunsPanel scheduleId="s-1" />);
  expect(screen.getByText("Uncertain")).toBeInTheDocument();
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
});


it("distinguishes recovered usage from verified delivery", () => {
  mocks.runs.mockReturnValue({ data: [run({ verification: "reconciled", status: "failed", reason: "error" })], isPending: false });
  wrap(<RunsPanel scheduleId="s-1" />);
  expect(screen.getByText("Usage reconciled")).toBeInTheDocument();
  expect(screen.queryByText("Delivery verified")).not.toBeInTheDocument();
});
