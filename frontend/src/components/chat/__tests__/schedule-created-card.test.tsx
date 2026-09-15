import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";

// Scheduled Jobs platform, Task 7 — the chat hand-off. `schedule.create`'s
// result_summary is the allowlisted JSON `summarize_tool_result` produces
// (backend/app/services/chat/tool_call_results.py, "schedule.create" branch)
// — same precedent as ChangeProposalCard's parseResult() for
// workspace_propose_patch.

import { ScheduleCreatedCard, parseScheduleCreated } from "@/components/chat/schedule-created-card";

describe("parseScheduleCreated", () => {
  it("parses a full JSON result_summary", () => {
    const summary = JSON.stringify({
      schedule_id: "s-9",
      name: "Payout reconciliation weekly",
      schedule_type: "job",
      plan_status: "pending_approval",
      summary_line: "3 steps · reads Stripe + NetSuite · writes an email",
    });
    const parsed = parseScheduleCreated(summary);
    expect(parsed?.schedule_id).toBe("s-9");
    expect(parsed?.name).toBe("Payout reconciliation weekly");
  });

  it("returns null for a clarification result (no schedule_id — nothing was created)", () => {
    const summary = "Which subsidiary?";
    expect(parseScheduleCreated(summary)).toBeNull();
  });

  it("extracts schedule_id via regex from truncated JSON", () => {
    const truncated = '{"schedule_id": "s-9", "name": "Payout reco';
    const parsed = parseScheduleCreated(truncated);
    expect(parsed?.schedule_id).toBe("s-9");
  });
});

describe("ScheduleCreatedCard", () => {
  it("links to the job on Scheduled jobs, in-app (not a new tab)", () => {
    render(<ScheduleCreatedCard data={{ schedule_id: "s-9", name: "Payout reconciliation weekly" }} />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/scheduled-jobs/s-9");
    expect(link).not.toHaveAttribute("target", "_blank");
  });

  it("shows the job name and the hand-off copy", () => {
    render(<ScheduleCreatedCard data={{ schedule_id: "s-9", name: "Payout reconciliation weekly" }} />);
    expect(screen.getByText("Payout reconciliation weekly")).toBeInTheDocument();
    expect(screen.getByText("Review the plan on Scheduled jobs →")).toBeInTheDocument();
  });

  it("falls back to a generic name when none is given", () => {
    render(<ScheduleCreatedCard data={{ schedule_id: "s-9", name: "" }} />);
    expect(screen.getByText("New scheduled job")).toBeInTheDocument();
  });
});
