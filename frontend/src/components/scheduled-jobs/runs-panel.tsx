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
import Link from "next/link";
import { ErrorNotice, Pill, formatDuration, formatWhen, runStatusLabel, runStatusTone } from "./shared";
import { useScheduleRuns } from "@/hooks/use-scheduled-jobs";

function outputsSummary(outputs: Record<string, unknown>): string {
  const keys = Object.keys(outputs);
  if (keys.length === 0) return "—";
  return keys.join(" · ");
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function OutputLinks({ outputs }: { outputs: Record<string, unknown> }) {
  const links = new Map<string, string>();
  for (const [step, value] of Object.entries(outputs)) {
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const item = value as Record<string, unknown>;
    if (typeof item.report_id === "string" && UUID.test(item.report_id)) {
      links.set(`/reports/${item.report_id}`, `Report · ${step}${typeof item.version === "number" ? ` · v${item.version}` : ""}`);
    }
    for (const key of ["pdf_url", "xlsx_url"]) {
      if (typeof item[key] !== "string") continue;
      try {
        const url = new URL(item[key]);
        if (url.protocol === "https:" && ["drive.google.com", "docs.google.com"].includes(url.hostname) && !url.username && !url.password) {
          links.set(url.href, `${key === "pdf_url" ? "PDF" : "Excel"} · ${step}`);
        }
      } catch { /* Untrusted output strings are never links. */ }
    }
  }
  return <div className="flex flex-col gap-1">{Array.from(links).map(([href, label]) =>
    href.startsWith("/") ? <Link className="text-primary underline" href={href} key={href}>{label}</Link>
      : <a className="text-primary underline" href={href} key={href} target="_blank" rel="noopener noreferrer">{label}</a>
  )}</div>;
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
                  <th className="py-1 pr-2 font-medium">Mode</th>
                  <th className="py-1 pr-2 font-medium">When</th>
                  <th className="py-1 pr-2 font-medium">Took</th>
                  <th className="py-1 pr-2 font-medium">Ended</th>
                  <th className="py-1 pr-2 font-medium">Verification</th>
                  <th className="py-1 pr-2 font-medium">Outputs</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const duration = formatDuration(run.started_at, run.completed_at);
                  return (
                    <tr key={run.id} className="border-t">
                      <td className="py-1.5 pr-2">{run.execution_mode === "test" ? "Report test" : "Live run"}</td>
                      <td className="py-1.5 pr-2 tabular-nums">{formatWhen(run.started_at)}</td>
                      <td className="py-1.5 pr-2 tabular-nums">{duration ?? "—"}</td>
                      <td className="py-1.5 pr-2">
                        <span className="mr-2">{run.status}</span>
                        <Pill tone={runStatusTone(run.reason)}>{runStatusLabel(run.reason)}</Pill>
                        {run.detail && <span className="ml-1.5 font-mono text-[11px]">{run.detail}</span>}
                      </td>
                      <td className="py-1.5 pr-2">{run.verification === "verified" ? "Delivery verified" : run.verification === "uncertain" ? "Uncertain" : "Not verified"}</td>
                      <td className="py-1.5 pr-2 text-muted-foreground">
                        <OutputLinks outputs={run.outputs} />
                        <details className="mt-1"><summary className="cursor-pointer">Run evidence · v{run.plan_version ?? "—"}</summary>
                          <p className="mt-1 break-all">Run: {run.id}</p>
                          <p className="break-all">Correlation: {run.correlation_id ?? "—"}</p>
                          <p>Attempt: {run.attempt ?? "—"}</p><p>{outputsSummary(run.outputs)}</p>
                        </details>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="border-t px-3 py-2.5 text-[12px] text-muted-foreground">
        Execution success does not verify a business outcome. Delivery verification reflects supported provider read-back evidence; each run retains its plan version and correlation ID.
      </div>
    </div>
  );
}
