import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

// Scheduled Jobs platform, Task 7 (spec §B6, mock state three — "New job").
// Mocks the hooks module directly (jobs-list.test.tsx's established
// technique): the wizard's own step transitions are what's under test, not
// useCreateSchedule/useUpdateSchedule/useScheduledJob themselves (each
// already covered in use-scheduled-jobs.test.tsx).

const mocks = vi.hoisted(() => ({
  create: vi.fn(),
  scheduledJob: vi.fn(),
  update: vi.fn(),
}));

vi.mock("@/hooks/use-scheduled-jobs", () => ({
  useCreateSchedule: () => mocks.create(),
  useScheduledJob: () => mocks.scheduledJob(),
  useUpdateSchedule: () => mocks.update(),
}));

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: routerPush }) }));

import { ApiError } from "@/lib/api-client";
import { NewJob } from "@/components/scheduled-jobs/new-job";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function detail(overrides: Partial<ScheduleDetail> = {}): ScheduleDetail {
  return {
    id: "s-9",
    tenant_id: "t-1",
    name: "Payout reconciliation weekly",
    schedule_type: "job",
    cron_expression: null,
    is_active: true,
    parameters: null,
    instruction: "Every Friday at 6pm, run the payout reconciliation.",
    plan_status: "pending_approval",
    plan_version: 0,
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
    has_pending_plan: false,
    plan_json: {
      steps: [
        { id: "step-1", type: "recon.run", params: {} },
        { id: "step-2", type: "bigquery_sql", params: { query: "select 1" } },
      ],
    },
    pending_plan_json: null,
    pending_plan_reason: null,
    pending_plan_diff: [],
    owner_id: null,
    ...overrides,
  };
}

let createMutate: ReturnType<typeof vi.fn>;
let updateMutate: ReturnType<typeof vi.fn>;

beforeEach(() => {
  routerPush.mockClear();
  createMutate = vi.fn();
  updateMutate = vi.fn();
  mocks.create.mockReturnValue({ mutate: createMutate, isPending: false, isError: false, error: null });
  mocks.update.mockReturnValue({ mutate: updateMutate, isPending: false });
  mocks.scheduledJob.mockReturnValue({ data: undefined, isPending: true, isError: false });
});

it("step 1 shows the instruction textarea, the mock's hint copy, and Compile plan", () => {
  wrap(<NewJob />);
  expect(screen.getByText("New job · 1 of 2 · what should it do?")).toBeInTheDocument();
  expect(screen.getByRole("textbox")).toBeInTheDocument();
  expect(
    screen.getByText(
      "Plain language. Name the source, the output, where it should go, and what to do when something is off. The agent asks if something is missing before it compiles.",
    ),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Compile plan →" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Cancel" })).toHaveAttribute("href", "/scheduled-jobs");
});

it("Compile plan is disabled until an instruction is typed", () => {
  wrap(<NewJob />);
  expect(screen.getByRole("button", { name: "Compile plan →" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "email me a weekly summary" } });
  expect(screen.getByRole("button", { name: "Compile plan →" })).not.toBeDisabled();
});

it("Compile plan calls useCreateSchedule with the instruction", () => {
  wrap(<NewJob />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "email me a weekly summary" } });
  fireEvent.click(screen.getByRole("button", { name: "Compile plan →" }));
  expect(createMutate).toHaveBeenCalledWith(
    { instruction: "email me a weekly summary" },
    expect.objectContaining({ onSuccess: expect.any(Function), onError: expect.any(Function) }),
  );
});

it("a successful compile moves to step 2 and renders the compiled plan's steps", async () => {
  mocks.scheduledJob.mockReturnValue({ data: detail(), isPending: false, isError: false });
  createMutate.mockImplementation((_body, { onSuccess }) => onSuccess({ id: "s-9" }));
  wrap(<NewJob />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "email me a weekly summary" } });
  fireEvent.click(screen.getByRole("button", { name: "Compile plan →" }));

  await waitFor(() =>
    expect(screen.getByText("New job · 2 of 2 · review the plan, then schedule")).toBeInTheDocument(),
  );
  expect(screen.getByText("Run the reconciliation")).toBeInTheDocument();
  expect(screen.getByText("Query BigQuery")).toBeInTheDocument();
});

it("a 409 clarification renders the question with an answer box instead of the plan", async () => {
  createMutate.mockImplementation((_body, { onError }) =>
    onError(new ApiError(JSON.stringify({ clarification: "Which subsidiary?" }), 409)),
  );
  wrap(<NewJob />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "email me a weekly summary" } });
  fireEvent.click(screen.getByRole("button", { name: "Compile plan →" }));

  await waitFor(() => expect(screen.getByText(/Which subsidiary\?/)).toBeInTheDocument());
  expect(screen.queryByText("New job · 2 of 2 · review the plan, then schedule")).toBeInTheDocument();
  expect(screen.queryByText("Run the reconciliation")).toBeNull();
  expect(screen.getByRole("textbox")).toBeInTheDocument(); // the answer box
});

it("answering the clarification re-compiles with the instruction plus the answer", async () => {
  createMutate.mockImplementationOnce((_body, { onError }) =>
    onError(new ApiError(JSON.stringify({ clarification: "Which subsidiary?" }), 409)),
  );
  wrap(<NewJob />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "email me a weekly summary" } });
  fireEvent.click(screen.getByRole("button", { name: "Compile plan →" }));
  await waitFor(() => expect(screen.getByText(/Which subsidiary\?/)).toBeInTheDocument());

  mocks.scheduledJob.mockReturnValue({ data: detail(), isPending: false, isError: false });
  createMutate.mockImplementationOnce((_body, { onSuccess }) => onSuccess({ id: "s-9" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Framework Inc" } });
  fireEvent.click(screen.getByRole("button", { name: "Compile plan →" }));

  expect(createMutate).toHaveBeenLastCalledWith(
    { instruction: "email me a weekly summary\n\nFramework Inc" },
    expect.anything(),
  );
  await waitFor(() =>
    expect(screen.getByText("New job · 2 of 2 · review the plan, then schedule")).toBeInTheDocument(),
  );
  expect(screen.getByText("Run the reconciliation")).toBeInTheDocument();
});

it("step 2 has a schedule + delivery form and Save PATCHes then routes to the list", async () => {
  mocks.scheduledJob.mockReturnValue({ data: detail(), isPending: false, isError: false });
  createMutate.mockImplementation((_body, { onSuccess }) => onSuccess({ id: "s-9" }));
  updateMutate.mockImplementation((_body, opts) => opts?.onSuccess?.());
  wrap(<NewJob />);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "email me a weekly summary" } });
  fireEvent.click(screen.getByRole("button", { name: "Compile plan →" }));
  await waitFor(() =>
    expect(screen.getByText("New job · 2 of 2 · review the plan, then schedule")).toBeInTheDocument(),
  );

  expect(screen.getByText("Schedule")).toBeInTheDocument();
  expect(screen.getByText("Delivery")).toBeInTheDocument();
  expect(screen.getByLabelText("Email to")).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Email to"), { target: { value: "ops@framework.computer" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  expect(updateMutate).toHaveBeenCalledWith(
    expect.objectContaining({
      cron_expression: expect.any(String),
      timezone: expect.any(String),
      delivery: { email: { to: "ops@framework.computer" } },
    }),
    expect.objectContaining({ onSuccess: expect.any(Function) }),
  );
  expect(routerPush).toHaveBeenCalledWith("/scheduled-jobs");
});
