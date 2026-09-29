"use client";

import { Button } from "@/components/ui/button";
import { useApproveSchedule, useTestSchedule, useValidateSchedule } from "@/hooks/use-scheduled-jobs";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

/** Parent keys this panel by every reviewed field so changed data clears validation. */
export function ReviewPanel({ schedule, onReload }: { schedule: ScheduleDetail; onReload?: () => void }) {
  const validation = useValidateSchedule(schedule.id);
  const approve = useApproveSchedule(schedule.id);
  const test = useTestSchedule(schedule.id);
  const pending = Boolean(schedule.pending_plan_json);
  const displayedHash = pending ? schedule.pending_plan_hash : schedule.plan_hash;
  const needsApproval = pending || schedule.plan_status === "pending_approval" || schedule.plan_status === "approved";
  const failed = Boolean(validation.error || approve.error || test.error);
  const review = failed ? undefined : validation.data;
  const busy = validation.isPending || approve.isPending || test.isPending;
  return (
    <section className="rounded-xl border bg-card p-5 shadow-soft" aria-label="Plan review">
      <h3 className="text-[15px] font-semibold">Validate and approve</h3>
      <p className="mt-2 text-[13px] text-muted-foreground">
        Validation checks inputs and saved source, owner permission and policy state. It does not execute steps or prove current credential health.
      </p>
      {pending && <p className="mt-2 text-[13px]">Reviewing the pending change; the approved plan remains in use.</p>}
      {review && <div className="mt-3 space-y-2 text-[13px]" aria-live="polite">
        <p>{review.ready ? "Ready for the reviewed operation" : "Plan needs attention"}</p>
        {[...review.blockers, ...(review.readiness_blockers ?? [])].map((b, i) => <p key={i} className="text-destructive">{b}</p>)}
        <p>Sources: {review.sources?.join(", ") || "In-app evidence and outputs"}</p>
        {review.source_bindings?.map((source) => <p key={source.id} className="break-all text-xs text-muted-foreground">{source.provider} · {source.id} · {source.status}</p>)}
        <p className="break-words">Owner permissions: {review.required_permissions?.join(", ")}</p>
        {review.steps?.map((step) => <details key={step.id}><summary className="cursor-pointer">Inputs · {step.id} · {step.type}</summary><pre className="mt-2 whitespace-pre-wrap break-all rounded border p-3 text-xs">{JSON.stringify(step.params, null, 2)}</pre></details>)}
        {review.test_supported ? <p>Report test: at most {review.test_seconds} seconds. Reads connected data and saves a new preview with automatic refresh off. No external delivery or schedule activation.</p> : <p>Test unavailable: {review.test_blockers?.join(" ")}</p>}
        {review.notes.map((note, i) => <p key={i} className="text-muted-foreground">{note}</p>)}
      </div>}
      <div className="mt-4 flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={busy || !displayedHash} onClick={() => { approve.reset(); test.reset(); validation.mutate({ usePending: pending, expectedPlanHash: displayedHash! }); }}>Validate plan</Button>
        {failed && onReload && <Button size="sm" variant="outline" disabled={busy} onClick={() => { validation.reset(); approve.reset(); test.reset(); onReload(); }}>Reload workflow</Button>}
        <Button size="sm" variant="outline" disabled={busy || !review?.ready || !review.test_supported || review.plan_hash !== displayedHash || Boolean(schedule.paused_at)} onClick={() => test.mutate({ use_pending: pending, expected_plan_hash: review!.plan_hash, readiness_hash: review!.readiness_hash })}>Run report test</Button>
        {needsApproval && <Button size="sm" disabled={busy || !displayedHash || !review?.ready || review.use_pending !== pending || review.plan_hash !== displayedHash}
          onClick={() => approve.mutate({ plan_hash: review!.plan_hash, readiness_hash: review!.readiness_hash })}>
          {pending ? "Approve pending plan" : schedule.plan_status === "approved" ? "Refresh approval" : "Approve plan"}
        </Button>}
      </div>
      {needsApproval && <p className="mt-3 text-xs text-muted-foreground">
        Approval permits live execution of this exact plan. An active cadence starts future runs; paused workflows remain paused.
      </p>}
      {test.data && <p role="status" className="mt-3 text-[13px]">Test queued. Its result and preview will appear in Runs below. Approval is unchanged.</p>}
      {(validation.error || approve.error || test.error) && <p role="alert" className="mt-3 text-[13px] text-destructive">
        {(validation.error || approve.error || test.error)?.message}
      </p>}
    </section>
  );
}
