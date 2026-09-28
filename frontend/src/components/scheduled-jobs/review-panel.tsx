"use client";

import { Button } from "@/components/ui/button";
import { useApproveSchedule, useValidateSchedule } from "@/hooks/use-scheduled-jobs";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

/** Parent keys this panel by every reviewed field so changed data clears validation. */
export function ReviewPanel({ schedule }: { schedule: ScheduleDetail }) {
  const validation = useValidateSchedule(schedule.id);
  const approve = useApproveSchedule(schedule.id);
  const pending = Boolean(schedule.pending_plan_json);
  const displayedHash = pending ? schedule.pending_plan_hash : schedule.plan_hash;
  const needsApproval = pending || schedule.plan_status === "pending_approval";
  const review = validation.data;
  const busy = validation.isPending || approve.isPending;
  return (
    <section className="rounded-xl border bg-card p-5 shadow-soft" aria-label="Plan review">
      <h3 className="text-[15px] font-semibold">Validate and approve</h3>
      <p className="mt-2 text-[13px] text-muted-foreground">
        Validation checks plan structure and inputs. It does not execute steps, test source access or verify an outcome.
      </p>
      {pending && <p className="mt-2 text-[13px]">Reviewing the pending change; the approved plan remains in use.</p>}
      {review && <div className="mt-3 space-y-2 text-[13px]" aria-live="polite">
        <p>{review.structurally_valid ? "Structure checks passed" : "Plan needs attention"}</p>
        {review.blockers.map((b, i) => <p key={i} className="text-destructive">{b}</p>)}
        {review.notes.map((note, i) => <p key={i} className="text-muted-foreground">{note}</p>)}
      </div>}
      <div className="mt-4 flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={busy || !displayedHash} onClick={() => validation.mutate({ usePending: pending, expectedPlanHash: displayedHash! })}>Validate plan</Button>
        {needsApproval && <Button size="sm" disabled={busy || !displayedHash || !review?.structurally_valid || review.use_pending !== pending || review.plan_hash !== displayedHash}
          onClick={() => approve.mutate(review!.plan_hash)}>
          {pending ? "Approve pending plan" : "Approve plan"}
        </Button>}
      </div>
      {needsApproval && <p className="mt-3 text-xs text-muted-foreground">
        Approval permits live execution of this exact plan. An active cadence starts future runs; paused workflows remain paused.
      </p>}
      {(validation.error || approve.error) && <p role="alert" className="mt-3 text-[13px] text-destructive">
        {(validation.error || approve.error)?.message}
      </p>}
    </section>
  );
}
