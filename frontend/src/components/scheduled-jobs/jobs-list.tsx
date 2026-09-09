"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6, mock state one — "the list").
 * Every job for the tenant, system jobs included, with what it does, when it
 * runs, and how the last run ended.
 *
 * Data sources, all pre-existing endpoints (no backend change in this task):
 * - `useScheduledJobs()` — `GET /api/v1/schedules`, this tenant's own rows
 *   (legacy `sync|report|recon` schedules and Scheduled Jobs alike).
 * - `useJobSchedules()` — `GET /api/v1/jobs/schedules`, the platform's own
 *   Celery Beat entries (the mock's "system" rows, e.g. "Report
 *   auto-refresh"). A DIFFERENT table entirely from `schedules` — there is
 *   no `schedule_type == "system"`. This query is gated on `tenant.manage`
 *   server-side (a narrower permission than this page's own
 *   `schedules.manage`), so a `schedules.manage`-only viewer may see it
 *   settle to an error; that's treated as "no system rows to show" rather
 *   than failing the whole page — the tenant's own jobs are still the
 *   primary content here.
 * - `usePlanInfo()` — `GET /api/v1/tenants/me/plan`, for the Jobs tile's
 *   quota sub-line.
 *
 * Two honest simplifications versus the mock's illustrative numbers, both
 * because the actual API shape (built by Tasks 3/4) doesn't carry the
 * underlying data — see this file's PR description for the fuller case:
 * - The mock's "Last run" cell shows a duration ("1m 52s"); `ScheduleResponse`
 *   carries only `last_run_at`/`last_run_status` (no duration), so this page
 *   shows the pill + when, not a duration it doesn't have.
 * - The mock's "Last 7 days" tile counts individual RUNS; `GET /schedules`
 *   carries only each schedule's MOST RECENT run, so this page counts
 *   distinct schedules that ran in the trailing 7 days, not total runs.
 */

import Link from "next/link";
import type { JSX } from "react";
import { useJobSchedules } from "@/hooks/use-jobs";
import { usePlanInfo } from "@/hooks/use-plan";
import {
  useResumeScheduledJob,
  useRunScheduleNow,
  useScheduledJobs,
  type ScheduledJob,
} from "@/hooks/use-scheduled-jobs";
import { queryState } from "@/lib/query-state";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  describeCron,
  describeDelivery,
  ErrorNotice,
  formatCountdown,
  formatWhen,
  KindTags,
  Pill,
  runStatusLabel,
  runStatusTone,
  SystemPill,
  Tile,
} from "./shared";

const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000;

const EMPTY_COPY = "No scheduled jobs yet. Describe one in plain language, or ask the chat to schedule something.";
const FOOTER_HINT =
  "System jobs are the platform's own schedules (sync, refresh, health). They are shown so nothing runs invisibly, but only their history is yours to read. A job is never deleted by a run; pausing keeps its history and its last outputs.";

// ---------------------------------------------------------------------------
// Row action — Run now / Resume / Open, mutually exclusive per the mock.
// ---------------------------------------------------------------------------

function RowAction({ job }: { job: ScheduledJob }): JSX.Element {
  const runNow = useRunScheduleNow();
  const resume = useResumeScheduledJob();

  if (job.paused_at) {
    return (
      <Button variant="ghost" size="sm" disabled={resume.isPending} onClick={() => resume.mutate(job.id)}>
        Resume
      </Button>
    );
  }
  const approved = job.plan_status === "approved";
  return (
    <Button
      variant="ghost"
      size="sm"
      disabled={runNow.isPending || !approved}
      title={approved ? undefined : "Approve the compiled plan before running it"}
      onClick={() => runNow.mutate(job.id)}
    >
      Run now
    </Button>
  );
}

// ---------------------------------------------------------------------------
// Table rows
// ---------------------------------------------------------------------------

function JobRow({ job }: { job: ScheduledJob }): JSX.Element {
  const schedule = describeCron(job.cron_expression);
  const delivery = describeDelivery(job.delivery_json);
  const next = job.next_run_at;
  return (
    <tr className="border-b last:border-0">
      <td className="px-2.5 py-2 align-top">
        <Link href={`/scheduled-jobs/${job.id}`} className="font-medium hover:underline">
          {job.name}
        </Link>
      </td>
      <td className="px-2.5 py-2 align-top">
        <div className="flex items-center gap-1">
          <KindTags kinds={job.kinds} />
        </div>
        {job.summary_line && <div className="mt-0.5 text-[11.5px] text-muted-foreground">{job.summary_line}</div>}
      </td>
      <td className="px-2.5 py-2 align-top">
        <div>{schedule.main}</div>
        {schedule.sub && <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">{schedule.sub}</div>}
      </td>
      <td className="px-2.5 py-2 align-top">
        <Pill tone={runStatusTone(job.last_run_status)}>{runStatusLabel(job.last_run_status)}</Pill>
        <div className="mt-0.5 text-[11.5px] text-muted-foreground">
          {job.paused_at ? job.pause_reason ?? "paused" : formatWhen(job.last_run_at)}
        </div>
      </td>
      <td className="px-2.5 py-2 align-top">
        <div>{next ? formatWhen(next) : "—"}</div>
        {next && <div className="mt-0.5 text-[11.5px] text-muted-foreground">{formatCountdown(next) ?? ""}</div>}
      </td>
      <td className="px-2.5 py-2 align-top">
        <div>{delivery.label}</div>
        {delivery.sub && <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">{delivery.sub}</div>}
      </td>
      <td className="px-2.5 py-2 align-top text-right">
        <RowAction job={job} />
      </td>
    </tr>
  );
}

function SystemRow({
  entry,
}: {
  entry: { name: string; task: string; schedule: string; enabled: boolean };
}): JSX.Element {
  return (
    <tr className="border-b bg-muted/30 last:border-0">
      <td className="px-2.5 py-2 align-top">
        <span className="font-medium">{entry.name}</span>
        <div className="mt-0.5">
          <SystemPill />
        </div>
      </td>
      <td className="px-2.5 py-2 align-top text-[11.5px] text-muted-foreground">{entry.task}</td>
      <td className="px-2.5 py-2 align-top text-[11.5px] text-muted-foreground">{entry.schedule}</td>
      <td className="px-2.5 py-2 align-top text-[11.5px] text-muted-foreground">—</td>
      <td className="px-2.5 py-2 align-top text-[11.5px] text-muted-foreground">—</td>
      <td className="px-2.5 py-2 align-top text-[11.5px] text-muted-foreground">—</td>
      <td className="px-2.5 py-2 align-top text-right">
        <Button variant="ghost" size="sm" disabled title="System jobs are read-only here">
          Open
        </Button>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

export function ScheduledJobsList(): JSX.Element {
  const jobsQuery = useScheduledJobs();
  const systemQuery = useJobSchedules();
  const planQuery = usePlanInfo();

  const jobsState = queryState(jobsQuery);
  // System rows and plan usage degrade gracefully: this page's own gate is
  // `schedules.manage`, but `GET /jobs/schedules` requires the narrower
  // `tenant.manage` — a viewer without it still gets their own jobs list.
  const systemState = queryState(systemQuery);
  const systemRows = systemState === "success" ? systemQuery.data ?? [] : [];

  let body: JSX.Element;
  if (jobsState === "pending") {
    body = (
      <div className="space-y-2" aria-busy="true">
        <span className="sr-only">Loading scheduled jobs…</span>
        <Skeleton className="h-9 w-full rounded-lg" />
        <Skeleton className="h-9 w-full rounded-lg" />
        <Skeleton className="h-9 w-full rounded-lg" />
      </div>
    );
  } else if (jobsState === "error") {
    body = <ErrorNotice message="Couldn't load scheduled jobs." onRetry={() => jobsQuery.refetch()} />;
  } else {
    const jobs = jobsQuery.data ?? [];
    const totalRows = jobs.length + systemRows.length;

    if (totalRows === 0) {
      body = <p className="text-[13px] text-muted-foreground">{EMPTY_COPY}</p>;
    } else {
      body = (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full border-collapse text-[12.5px]">
            <thead>
              <tr className="border-b bg-muted text-left text-[10.5px] uppercase tracking-wide text-muted-foreground">
                <th className="px-2.5 py-1.5 font-medium">Job</th>
                <th className="px-2.5 py-1.5 font-medium">Does</th>
                <th className="px-2.5 py-1.5 font-medium">Schedule</th>
                <th className="px-2.5 py-1.5 font-medium">Last run</th>
                <th className="px-2.5 py-1.5 font-medium">Next</th>
                <th className="px-2.5 py-1.5 font-medium">Delivers to</th>
                <th className="px-2.5 py-1.5 font-medium" />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <JobRow key={job.id} job={job} />
              ))}
              {systemRows.map((entry) => (
                <SystemRow key={entry.name} entry={entry} />
              ))}
            </tbody>
          </table>
        </div>
      );
    }
  }

  return (
    <div className="space-y-4 animate-fade-in">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">Scheduled jobs</h2>
        </div>
        <div className="ml-auto flex gap-2">
          <Button variant="outline" size="sm">
            Run history
          </Button>
          <Button asChild size="sm">
            <Link href="/scheduled-jobs/new">+ New job</Link>
          </Button>
        </div>
      </div>

      <TilesRow jobsState={jobsState} jobs={jobsQuery.data} systemCount={systemRows.length} planQuery={planQuery} />

      {body}

      <p className="text-[12px] text-muted-foreground">{FOOTER_HINT}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tiles — computed purely from the list response + plan usage (spec §B6);
// never rendered with a real number until the underlying query has settled.
// ---------------------------------------------------------------------------

function TilesRow({
  jobsState,
  jobs,
  systemCount,
  planQuery,
}: {
  jobsState: ReturnType<typeof queryState>;
  jobs: ScheduledJob[] | undefined;
  systemCount: number;
  planQuery: ReturnType<typeof usePlanInfo>;
}): JSX.Element {
  if (jobsState !== "success") {
    return (
      <dl className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <Tile label="Jobs" value="—" />
        <Tile label="Last 7 days" value="—" />
        <Tile label="Needs attention" value="—" />
        <Tile label="Next run" value="—" />
      </dl>
    );
  }

  const rows = jobs ?? [];
  const yourCount = rows.length;
  const total = yourCount + systemCount;

  const planState = queryState(planQuery);
  const quotaSub =
    planState === "success" && planQuery.data
      ? `${planQuery.data.usage.schedules} of ${planQuery.data.limits.max_schedules} in your plan's quota`
      : undefined;

  const now = Date.now();
  const recentRuns = rows.filter((j) => j.last_run_at && now - new Date(j.last_run_at).getTime() <= SEVEN_DAYS_MS);
  const recentDone = recentRuns.filter((j) => j.last_run_status === "done").length;
  const recentNotDone = recentRuns.length - recentDone;

  const needsAttention = rows.filter((j) => j.plan_status === "pending_approval" || j.paused_at).length;

  const upcoming = rows
    .filter((j) => j.plan_status === "approved" && !j.paused_at && j.next_run_at)
    .sort((a, b) => new Date(a.next_run_at!).getTime() - new Date(b.next_run_at!).getTime())[0];

  return (
    <dl className="grid grid-cols-2 gap-2 md:grid-cols-4">
      <Tile label="Jobs" value={`${total} · ${yourCount} yours, ${systemCount} system`} sub={quotaSub} />
      <Tile
        label="Last 7 days"
        value={`${recentRuns.length} run${recentRuns.length === 1 ? "" : "s"}`}
        sub={recentRuns.length > 0 ? `${recentDone} done · ${recentNotDone} not done` : undefined}
        tone={recentNotDone > 0 ? "warn" : recentRuns.length > 0 ? "ok" : undefined}
      />
      <Tile
        label="Needs attention"
        value={needsAttention}
        sub={needsAttention > 0 ? "needs your review" : undefined}
        tone={needsAttention > 0 ? "warn" : undefined}
      />
      <Tile
        label="Next run"
        value={upcoming ? formatWhen(upcoming.next_run_at) : "—"}
        sub={upcoming ? `${upcoming.name} · ${formatCountdown(upcoming.next_run_at) ?? ""}` : undefined}
      />
    </dl>
  );
}
