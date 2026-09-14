import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { DeliveryPanel } from "@/components/scheduled-jobs/delivery-panel";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

// Scheduled Jobs platform, Task 6 (spec §B6, mock state two — "Delivery").
// Pure presentational — no hooks, no mutation (the brief lists "delivery
// panel" alongside the wired panels but doesn't call for a wired Edit here;
// this reads `delivery_json` honestly, the same simplification precedent
// jobs-list.tsx's own file docstring documents for schema-less fields).

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
    delivery_json: { drive: { folder: "Reports / Inventory aging" } },
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

it("shows the Drive folder when delivery_json.drive is set", () => {
  render(<DeliveryPanel schedule={detail()} />);
  expect(screen.getByText("Drive")).toBeInTheDocument();
  expect(screen.getByText("Reports / Inventory aging")).toBeInTheDocument();
});

it("shows the email recipients when delivery_json.email is set", () => {
  render(<DeliveryPanel schedule={detail({ delivery_json: { email: { to: "ops@example.com", count: 2 } } })} />);
  expect(screen.getByText("Email")).toBeInTheDocument();
  expect(screen.getByText(/ops@example\.com/)).toBeInTheDocument();
  expect(screen.getByText(/2 recipients/)).toBeInTheDocument();
});

it("shows a no-delivery message when delivery_json is null", () => {
  render(<DeliveryPanel schedule={detail({ delivery_json: null })} />);
  expect(screen.getByText(/No delivery configured/)).toBeInTheDocument();
});
