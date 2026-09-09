import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { PlanPanel } from "@/components/scheduled-jobs/plan-panel";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

// Scheduled Jobs platform, Task 6 (spec §B6, mock state two — "Compiled
// plan"). Pure presentational component (no hooks) — plan_json.steps in,
// numbered step rows + the verbatim registry note out.

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
    has_pending_plan: false,
    plan_json: {
      steps: [
        { id: "q1", type: "bigquery_sql", params: { query: "SELECT * FROM aging" } },
        { id: "compose", type: "report.compose", params: { playbook_key: "inventory_aging" } },
        { id: "pdf", type: "report.render_pdf", params: { report_step: "compose" } },
        { id: "xlsx", type: "report.build_xlsx", params: { report_step: "compose" } },
        { id: "upload", type: "drive.upload", params: { report_step: "compose" } },
      ],
    },
    pending_plan_json: null,
    pending_plan_reason: null,
    pending_plan_diff: [],
    owner_id: null,
    ...overrides,
  };
}

it("numbers each step and shows its title, description, and READ/WRITE tag", () => {
  render(<PlanPanel schedule={detail()} />);
  expect(screen.getByText("1")).toBeInTheDocument();
  expect(screen.getByText("5")).toBeInTheDocument();
  expect(screen.getByText(/SELECT \* FROM aging/)).toBeInTheDocument();
  expect(screen.getByText("Upload to Google Drive")).toBeInTheDocument();
  const writeTags = screen.getAllByText("WRITE");
  expect(writeTags.length).toBe(1); // only drive.upload
  const allowListedPills = screen.getAllByText("allow-listed");
  expect(allowListedPills.length).toBe(1); // only the write step carries it
});

it("shows the plan version and status pill", () => {
  render(<PlanPanel schedule={detail({ plan_version: 3, plan_status: "approved" })} />);
  expect(screen.getByText(/v3/)).toBeInTheDocument();
  expect(screen.getByText(/approved/)).toBeInTheDocument();
});

it("shows the 'What the agent may not do here' note verbatim", () => {
  render(<PlanPanel schedule={detail()} />);
  expect(screen.getByText(/What the agent may not do here:/)).toBeInTheDocument();
  expect(
    screen.getByText(
      /steps come only from the job registry \(queries, reports, files, Drive, email, recon runs\)\. A NetSuite or Celigo write cannot appear in a scheduled plan today; when it can, it will require the same confirmation card the chat uses, on a human's screen, before the job may include it\./,
    ),
  ).toBeInTheDocument();
});

it("shows a no-plan message when plan_json has no steps yet", () => {
  render(<PlanPanel schedule={detail({ plan_json: null })} />);
  expect(screen.getByText(/No compiled plan yet/)).toBeInTheDocument();
});
