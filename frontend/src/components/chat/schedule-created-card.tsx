"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6 — the chat hand-off). Renders
 * inline after the agent calls `schedule.create` (`app/mcp/tools/
 * schedule_ops.py::execute_create`), whether the operator typed "schedule
 * this" or the plan was compiled on the Scheduled jobs page's own New job
 * flow via a tool call. Nothing is scheduled from a chat turn alone — the
 * row already exists (`plan_status: "pending_approval"`, spec §B6), and
 * this card's only job is to hand the operator off to the same
 * compile-then-approve review `new-job.tsx` step 2 and `job-detail.tsx`
 * both show, exactly like `ReportReadyCard` hands off to `/reports/{id}`.
 *
 * `parseScheduleCreated` reads `step.result_summary`, the allowlisted JSON
 * `summarize_tool_result`'s `schedule.create` branch produces
 * (`backend/app/services/chat/tool_call_results.py`) — same precedent as
 * `change-proposal-card.tsx`'s `parseResult()` for `workspace_propose_patch`,
 * regex fallback included for the same reason: a persisted summary can be
 * truncated upstream of this allowlist ever landing (an older message, a
 * future re-truncation), and a missing/failed parse must fall back to the
 * generic `ToolCallStepCard` rather than throw. A clarification result
 * (`{"error": true, "clarification": true, "message": ...}`, or any other
 * tool failure) has no `schedule_id` — nothing was created — so it returns
 * `null` here and the agent's own text relays the question instead.
 */

import Link from "next/link";
import { CalendarClock, ArrowRight } from "lucide-react";

export interface ScheduleCreatedData {
  schedule_id: string;
  name?: string;
  schedule_type?: string;
  plan_status?: string;
  summary_line?: string;
}

export function parseScheduleCreated(resultSummary: string): ScheduleCreatedData | null {
  try {
    const parsed = JSON.parse(resultSummary);
    if (typeof parsed?.schedule_id === "string" && parsed.schedule_id) {
      return parsed as ScheduleCreatedData;
    }
    return null;
  } catch {
    const match = resultSummary.match(/"schedule_id":\s*"([^"]+)"/);
    if (!match) return null;
    const nameMatch = resultSummary.match(/"name":\s*"([^"]*)"/);
    const summaryMatch = resultSummary.match(/"summary_line":\s*"([^"]*)"/);
    return { schedule_id: match[1], name: nameMatch?.[1], summary_line: summaryMatch?.[1] };
  }
}

export function ScheduleCreatedCard({ data }: { data: ScheduleCreatedData }) {
  return (
    <Link
      href={`/scheduled-jobs/${data.schedule_id}`}
      aria-label={`Review the plan for ${data.name || "the new scheduled job"}`}
      className="flex items-center gap-3 rounded-xl border bg-card p-4 shadow-soft hover:bg-accent/50 transition-colors"
    >
      <CalendarClock aria-hidden className="h-5 w-5 text-indigo-600 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-[15px] font-medium text-foreground truncate">{data.name || "New scheduled job"}</p>
        <p className="text-[13px] text-muted-foreground truncate">Review the plan on Scheduled jobs →</p>
      </div>
      <ArrowRight aria-hidden className="h-4 w-4 text-muted-foreground shrink-0" />
    </Link>
  );
}
