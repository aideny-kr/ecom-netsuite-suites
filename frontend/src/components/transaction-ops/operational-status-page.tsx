"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowLeft, Clock3, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import { useOperationalStatus } from "@/hooks/use-operational-status";
import { useTransactionAccess } from "@/hooks/use-transaction-ops";
import { TransactionAccessBoundary } from "./access-boundary";
import type { OperationalEntity, OperationalRun } from "./operational-status-types";

const labels: Record<string, string> = {
  up_to_date: "Caught up", behind: "Behind", not_verified: "Not verified",
  not_applicable: "Interval schedule", paused: "Paused",
  scheduled_check: "Next scheduled check", work_in_progress: "Work in progress",
  scheduler_recovery: "Scheduler recovery needed", collection_recheck: "Recheck collection wait",
  dispatch_pending: "Awaiting dispatch", continue_checkpoint: "Continue saved checkpoint",
  check_connection: "Connection check needed", operator_review: "Operator review needed",
  new_schedule_cycle: "New daily cycle eligible", verify_coverage: "Verify scan coverage",
  catch_up: "Catch-up eligible", initial_scan: "Initial scan eligible",
  waiting_for_daily_cutoff: "Waiting for the next daily cutoff",
  active_lease: "Run has an active lease", recorded_collection_wait: "Saved wait needs a scheduler recheck",
  queued_run: "Run is queued", scheduler_scope_due: "A scan window is due",
  authentication_rejected: "The connection needs an authentication check",
  schedule_disabled: "The schedule is disabled",
};
function label(value: string | null) {
  return value ? labels[value] || value.replaceAll("_", " ") : "Not recorded";
}
function timestamp(value: string | null) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Not recorded" : `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

function RunEvidence({ run, title }: { run: OperationalRun; title: string }) {
  const failure = run.last_read_failure;
  return (
    <div className="space-y-2 border-t pt-3 text-[13px]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-medium">{title}</p>
        <Link href={`/transaction-operations/runs/${encodeURIComponent(run.run_id)}`} className="underline underline-offset-4">View run</Link>
      </div>
      <p className="text-muted-foreground">Saved state updated: {timestamp(run.run_state_updated_at)}. This is not a progress measurement.</p>
      <p className="tabular-nums">{run.financial_counts.matched ?? "Unknown"} matched · {run.financial_counts.needs_review ?? "Unknown"} need review · {run.financial_counts.not_verified ?? "Unknown"} unverified</p>
      <p className="text-muted-foreground">Counts describe this run checkpoint, not period totals.</p>
      {failure && (
        <p className={failure.blocking ? "font-medium text-amber-700 dark:text-amber-300" : "text-muted-foreground"}>
          Read issue: {label(failure.code)} · {label(failure.stage)}. {failure.blocking ? "Blocking this run." : failure.resolved ? "Resolved; not blocking." : "Resolution not verified; not marked as blocking."}
        </p>
      )}
      {run.collection_wait && <p className="text-muted-foreground">A collection wait was recorded. The scheduler must recheck it before treating it as a current blocker.</p>}
    </div>
  );
}

function EntityStatus({ entity }: { entity: OperationalEntity }) {
  const attention = ["behind", "not_verified"].includes(entity.coverage.status);
  return (
    <article className="rounded-xl border bg-card p-5 shadow-soft" aria-label={entity.name}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">{entity.name}</h2>
          <p className="mt-1 text-[13px] text-muted-foreground">{entity.schedule.kind === "daily" ? "Daily schedule" : "Interval schedule"} · {entity.schedule.timezone}{!entity.schedule.enabled && " · Disabled"}</p>
        </div>
        <span className={`rounded-full border px-3 py-1 text-[13px] font-medium ${attention ? "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200" : "bg-muted text-foreground"}`}>
          {label(entity.coverage.status)}
        </span>
      </div>
      {entity.freshness?.state === "alert" && <p className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-[13px] font-medium text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100">{entity.freshness.reason === "daily_scan_stopped" ? "Freshness alert: daily scan stopped before coverage completed." : "Freshness alert: daily coverage missed its completion deadline."} Expected through {entity.coverage.expected_checked_through || "an unverified date"}. Completion deadline: {timestamp(entity.freshness.deadline_at)}.</p>}
      {entity.active_runs.some(run => run.last_read_failure?.blocking) && <p className="mt-3 text-[13px] font-medium text-amber-700 dark:text-amber-300">A read issue is blocking an active run. Open run evidence for details.</p>}
      <div className="mt-5 grid gap-6 md:grid-cols-3">
        <div>
          <h3 className="text-[13px] font-medium text-muted-foreground">Latest verified day</h3>
          <p className="mt-1 text-xl font-semibold tabular-nums">{entity.coverage.checked_through || "Not verified"}</p>
          <p className="mt-1 text-[13px] text-muted-foreground">{entity.coverage.expected_checked_through ? `Expected through ${entity.coverage.expected_checked_through}` : "Daily coverage does not apply to this schedule."}</p>
        </div>
        <div>
          <h3 className="text-[13px] font-medium text-muted-foreground">Current work</h3>
          {entity.active_runs.length ? (
            <ul className="mt-1 space-y-1 text-[15px]">
              {entity.active_runs.map(run => <li key={run.run_id}>{label(run.origin)} · {label(run.execution_state)}{run.phase && ` · ${label(run.phase)}`}</li>)}
            </ul>
          ) : <p className="mt-1 text-[15px]">No active run</p>}
          {entity.active_runs_truncated && <p className="mt-1 text-[13px] text-amber-700 dark:text-amber-300">More active runs exist; only the first five are shown.</p>}
        </div>
        <div>
          <h3 className="text-[13px] font-medium text-muted-foreground">Next action</h3>
          <p className="mt-1 text-[15px] font-medium">{label(entity.next_action.kind)}</p>
          <p className="mt-1 text-[13px] text-muted-foreground">{label(entity.next_action.reason)}</p>
          {entity.next_action.eligible_at && <p className="mt-2 flex items-start gap-1.5 text-[13px] tabular-nums"><Clock3 className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />Eligible {timestamp(entity.next_action.eligible_at)}</p>}
        </div>
      </div>
      {(entity.latest_schedule || entity.active_runs.length > 0) && (
        <details className="mt-5 border-t pt-3">
          <summary className="cursor-pointer text-[13px] font-medium">Run evidence and read issues</summary>
          <div className="mt-3 space-y-3">
            {entity.active_runs.map(run => <RunEvidence key={run.run_id} run={run} title={`Active ${label(run.origin)} run`} />)}
            {entity.latest_schedule && !entity.active_runs.some(run => run.run_id === entity.latest_schedule?.run_id) && <RunEvidence run={entity.latest_schedule} title="Latest scheduled checkpoint" />}
          </div>
        </details>
      )}
    </article>
  );
}

export function OperationalStatusPage() {
  const { tenantId } = useTransactionAccess();
  return <TransactionAccessBoundary><StatusContent key={tenantId} /></TransactionAccessBoundary>;
}

function StatusContent() {
  const [offsets, setOffsets] = useState([0]);
  const offset = offsets[offsets.length - 1];
  const query = useOperationalStatus(offset);
  const denied = query.error instanceof ApiError && [401, 403].includes(query.error.status);
  const data = denied ? undefined : query.data;
  return (
    <div className="animate-fade-in space-y-6 text-[15px]">
      <Link href="/settings" className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground"><ArrowLeft className="h-4 w-4" aria-hidden="true" />Settings</Link>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Ops status</h1>
          <p className="mt-1 text-muted-foreground">Reconciliation coverage, current work and the next eligible action.</p>
          <p className="mt-2 text-[13px] text-muted-foreground">{data ? `Snapshot: ${timestamp(data.observed_at)}` : "Reading saved reconciliation state"}</p>
        </div>
        <Button variant="outline" onClick={() => void query.refetch()} disabled={query.isFetching}>
          <RefreshCw className={`mr-2 h-4 w-4 ${query.isFetching ? "animate-spin" : ""}`} aria-hidden="true" />{query.isFetching ? "Refreshing…" : "Refresh status"}
        </Button>
      </header>
      <p className="rounded-xl border bg-muted/40 p-4 text-[13px] text-muted-foreground">Scan coverage is separate from accounting completion. A verified day does not prove every earlier day was scanned. Next actions require scheduler revalidation; eligibility is not a dispatch confirmation.</p>
      {query.isError && <p role="alert" className="rounded-xl border border-amber-300 p-4">{denied ? "You no longer have access to this status. Check your workspace permissions." : data ? "Refresh failed. The snapshot below is older; do not treat it as current status." : "Status could not be loaded. Use Refresh status to try again."}</p>}
      {query.isLoading && <p role="status">Loading reconciliation status…</p>}
      {data && (
        <>
          {data.entities.length === 0 ? <p className="rounded-xl border bg-card p-5">No reconciliation configurations are available on this page.</p> : (
            <div className="space-y-4">{data.entities.map(entity => <EntityStatus key={entity.config_id} entity={entity} />)}</div>
          )}
          <div className="flex flex-wrap items-center justify-between gap-3 text-[13px]">
            <p className="text-muted-foreground">{data.entities.length} {data.entities.length === 1 ? "entity" : "entities"} on this page{data.truncated ? " · More entities available" : ""}</p>
            <nav aria-label="Status pages" className="flex gap-2">
              <Button variant="outline" disabled={offsets.length === 1 || query.isFetching} onClick={() => setOffsets(values => values.slice(0, -1))}>Previous</Button>
              <Button variant="outline" disabled={!data.truncated || data.next_offset === null || query.isFetching} onClick={() => { if (data.next_offset !== null) setOffsets(values => [...values, data.next_offset!]); }}>Next</Button>
            </nav>
          </div>
        </>
      )}
      <footer className="border-t pt-4 text-[13px] text-muted-foreground">Source: saved reconciliation records. Refresh reads the saved state; it does not start a scan or contact NetSuite. Worker health and throughput are not measured here.</footer>
    </div>
  );
}
