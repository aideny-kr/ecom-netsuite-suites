import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";
const mocks = vi.hoisted(() => ({ validate: vi.fn(), approve: vi.fn(), test: vi.fn() }));
vi.mock("@/hooks/use-scheduled-jobs", () => ({
  useTestSchedule: () => mocks.test(), useValidateSchedule: () => mocks.validate(), useApproveSchedule: () => mocks.approve(),
}));
import { ReviewPanel } from "../review-panel";
const schedule = { id: "s", plan_status: "pending_approval", plan_version: 0, pending_plan_json: null, plan_hash: "abc" } as ScheduleDetail;
const approve = vi.fn(), validate = vi.fn();
beforeEach(() => {
  vi.clearAllMocks();
  mocks.test.mockReturnValue({ mutate: vi.fn(), reset: vi.fn(), isPending: false });
  mocks.approve.mockReturnValue({ mutate: approve, reset: vi.fn(), isPending: false });
  mocks.validate.mockReturnValue({ mutate: validate, isPending: false, data: undefined });
});
it("requires explicit validation before first approval", () => {
  render(<ReviewPanel schedule={schedule} />);
  expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Validate plan" }));
  expect(validate).toHaveBeenCalledWith({ usePending: false, expectedPlanHash: "abc" });
  expect(approve).not.toHaveBeenCalled();
});
it("sends the reviewed fingerprint rather than approving whichever plan is current", () => {
  mocks.validate.mockReturnValue({ mutate: validate, data: { plan_hash: "abc", ready: true, readiness_hash: "ready", test_supported: true, test_blockers: [], required_permissions: [], sources: [], readiness_blockers: [], steps: [], structurally_valid: true, blockers: [], notes: [], use_pending: false } });
  render(<ReviewPanel schedule={schedule} />);
  fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));
  expect(approve).toHaveBeenCalledWith({ plan_hash: "abc", readiness_hash: "ready" });
});
it("shows blockers and cannot approve an invalid plan", () => {
  mocks.validate.mockReturnValue({ mutate: validate, data: { plan_hash: "abc", structurally_valid: false, blockers: ["Unsupported step"], notes: [] } });
  render(<ReviewPanel schedule={schedule} />);
  expect(screen.getByText("Unsupported step")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
});
it("labels validation as structure checks, not an executed test", () => {
  render(<ReviewPanel schedule={schedule} />);
  expect(screen.getByText(/does not execute/)).toBeInTheDocument();
});

it("cannot approve a validation for a different displayed snapshot", () => {
  mocks.validate.mockReturnValue({ mutate: validate, data: { plan_hash: "newer", ready: true, readiness_hash: "ready", test_supported: true, test_blockers: [], required_permissions: [], sources: [], readiness_blockers: [], steps: [], structurally_valid: true, blockers: [], notes: [], use_pending: false } });
  render(<ReviewPanel schedule={schedule} />);
  expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
});

it("test button sends the reviewed snapshot separately from approval", () => {
  const test = vi.fn();
  mocks.test.mockReturnValue({ mutate: test });
  mocks.validate.mockReturnValue({ data: { plan_hash: "abc", readiness_hash: "ready", ready: true, structurally_valid: true, test_supported: true, test_seconds: 60, use_pending: false, blockers: [], notes: [], readiness_blockers: [], test_blockers: [], sources: ["netsuite"], required_permissions: ["chat.financial_reports"], steps: [{ id:"r", type:"report.compose", params: { period: "Jun 2026" } }] } });
  render(<ReviewPanel schedule={schedule} />);
  fireEvent.click(screen.getByRole("button", { name: "Run report test" }));
  expect(test).toHaveBeenCalledWith({ use_pending: false, expected_plan_hash: "abc", readiness_hash: "ready" });
  expect(approve).not.toHaveBeenCalled();
  expect(screen.getByText(/Jun 2026/)).toBeInTheDocument();
});

it("invalidates a stale approval and offers an explicit reload", () => {
  const reload = vi.fn(), reset = vi.fn();
  mocks.approve.mockReturnValue({ error: new Error("Plan changed. Reload."), reset });
  mocks.validate.mockReturnValue({ reset, data: { plan_hash: "abc", ready: true } });
  render(<ReviewPanel schedule={schedule} onReload={reload} />);
  expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Run report test" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Reload workflow" }));
  expect(reload).toHaveBeenCalledOnce();
  expect(reset).toHaveBeenCalled();
});
