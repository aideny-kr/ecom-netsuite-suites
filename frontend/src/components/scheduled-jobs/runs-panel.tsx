"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6, mock state two — "Runs").
 * `GET /schedules/{id}/runs` (`ScheduleRunItem`, read from the `jobs`
 * table via `Job.parameters['schedule_id']`) — each row is one
 * `run_schedule_now` call, with a real `reason` (`done|budget|stall|error|
 * blocked`, the executor's own enum — never fabricated) and, unlike the
 * list page's "Last run" cell, a real duration (`formatDuration`, both
 * `started_at`/`completed_at` are on this response).
 */

import type { JSX } from "react";
import { ErrorNotice, Pill, formatDuration, formatWhen, runStatusLabel, runStatusTone } from "./shared";
import { useScheduleRuns } from "@/hooks/use-scheduled-jobs";

function outputsSummary(outputs: Record<string, unknown>): string {
  const keys = Object.keys(outputs);
  if (keys.length === 0) return "—";
  return keys.join(" · ");
}

export function RunsPanel({ scheduleId }: { scheduleId: string }): JSX.Element {
  const runsQuery = useScheduleRuns(scheduleId);
  const runs = runsQuery.data ?? [];

  return (
    <div className="rounded-lg border bg-card">
      <h3 className="border-b px-3 py-2 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        Runs
      </h3>
      <div className="p-3">
        {runsQuery.isPending ? (
          <p className="text-[13px] text-muted-foreground">Loading runs…</p>
        ) : runsQuery.isError ? (
          <ErrorNotice message="Couldn't load runs." onRetry={() => runsQuery.refetch()} />
        ) : runs.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No runs yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[12px]">
              <thead>
                <tr className="text-left text-[10.5px] uppercase tracking-wide text-muted-foreground">
                  <th className="py-1 pr-2 font-medium">When</th>
                  <th className="py-1 pr-2 font-medium">Took</th>
                  <th className="py-1 pr-2 font-medium">Ended</th>
                  <th className="py-1 pr-2 font-medium">Outputs</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const duration = formatDuration(run.started_at, run.completed_at);
                  return (
                    <tr key={run.id} className="border-t">
                      <td className="py-1.5 pr-2 tabular-nums">{formatWhen(run.started_at)}</td>
                      <td className="py-1.5 pr-2 tabular-nums">{duration ?? "—"}</td>
                      <td className="py-1.5 pr-2">
                        <Pill tone={runStatusTone(run.reason)}>{runStatusLabel(run.reason)}</Pill>
                        {run.detail && <span className="ml-1.5 font-mono text-[11px]">{run.detail}</span>}
                      </td>
                      <td className="py-1.5 pr-2 text-muted-foreground">{outputsSummary(run.outputs)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="border-t px-3 py-2.5 text-[12px] text-muted-foreground">
        Every run is a row in the jobs table with a correlation id, the plan version it used, what it scanned, what
        it wrote, and why it ended. The audit log links here.
      </div>
    </div>
  );
}
