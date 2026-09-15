import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

// Scheduled Jobs platform, Task 6 (spec §B6, mock state two — "Instruction").
// Mocks the hooks module directly, same technique jobs-list.test.tsx
// established: this component's own edit/save/clarification flow is what's
// under test, not `useUpdateSchedule` itself (already covered in
// use-scheduled-jobs.test.tsx).

const mocks = vi.hoisted(() => ({ update: vi.fn() }));

vi.mock("@/hooks/use-scheduled-jobs", () => ({
  useUpdateSchedule: () => mocks.update(),
}));

import { ApiError } from "@/lib/api-client";
import { InstructionPanel } from "@/components/scheduled-jobs/instruction-panel";
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
    instruction: "Every Monday at 6am Pacific, build the inventory aging report.",
    plan_status: "approved",
    plan_version: 3,
    timezone: "America/Los_Angeles",
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
    plan_json: null,
    pending_plan_json: null,
    pending_plan_reason: null,
    pending_plan_diff: [],
    owner_id: null,
    ...overrides,
  };
}

let mutate: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mutate = vi.fn();
  mocks.update.mockReturnValue({ mutate, isPending: false, isError: false, error: null });
});

it("shows the instruction text and the source-of-truth hint verbatim", () => {
  wrap(<InstructionPanel schedule={detail()} />);
  expect(screen.getByText(detail().instruction!)).toBeInTheDocument();
  expect(
    screen.getByText(
      /Written by you \(or by the chat when you said "schedule this"\)\. This is the source of truth; the plan below is compiled from it\. Numbers in the output never come from this text\./,
    ),
  ).toBeInTheDocument();
});

it("Edit opens a textarea seeded with the current instruction, and Save PATCHes it", async () => {
  wrap(<InstructionPanel schedule={detail()} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));

  const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
  expect(textarea.value).toBe(detail().instruction);

  fireEvent.change(textarea, { target: { value: "and Virtual" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  expect(mutate).toHaveBeenCalledWith({ instruction: "and Virtual" }, expect.any(Object));
});

it("Cancel discards the draft and returns to read mode without saving", () => {
  wrap(<InstructionPanel schedule={detail()} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "scrapped draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

  expect(mutate).not.toHaveBeenCalled();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByText(detail().instruction!)).toBeInTheDocument();
});

it('"Ask the agent to adjust…" also opens the editor', () => {
  wrap(<InstructionPanel schedule={detail()} />);
  fireEvent.click(screen.getByRole("button", { name: "Ask the agent to adjust…" }));
  expect(screen.getByRole("textbox")).toBeInTheDocument();
});

it("a 409 clarification from Save is shown above the textarea instead of a plain error", async () => {
  wrap(<InstructionPanel schedule={detail()} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  const [, opts] = mutate.mock.calls[0];
  const err = new ApiError('{"clarification":"Which NetSuite subsidiary?"}', 409);
  opts.onError(err);

  await waitFor(() => expect(screen.getByText(/Which NetSuite subsidiary\?/)).toBeInTheDocument());
  // still editing — the clarification doesn't silently exit edit mode
  expect(screen.getByRole("textbox")).toBeInTheDocument();
});
