"use client";

/** Pending plan diff, explicit one-time live execution, and discard.
 * Version-bound validation and approval live in ReviewPanel. */

import type { JSX } from "react";
import { Button } from "@/components/ui/button";
import { Pill } from "./shared";
import { useRunSchedule, useUpdateSchedule } from "@/hooks/use-scheduled-jobs";
import type { DiffLine, ScheduleDetail } from "@/hooks/use-scheduled-jobs";

function DiffLineRow({ line }: { line: DiffLine }): JSX.Element {
  const tone = line.kind === "add" ? "text-emerald-400" : line.kind === "del" ? "text-rose-300" : "text-neutral-500";
  return <div className={tone}>{line.text}</div>;
}

export function PendingChangePanel({ schedule }: { schedule: ScheduleDetail }): JSX.Element | null {
  const run = useRunSchedule(schedule.id);
  const update = useUpdateSchedule(schedule.id);

  if (!schedule.pending_plan_json) return null;

  const busy = run.isPending || update.isPending;

  return (
    <div className="rounded-lg border bg-card">
      <h3 className="flex items-center gap-2 border-b px-3 py-2 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        Pending change
        <span className="flex-1" />
        <Pill tone="warn">awaiting your approval</Pill>
      </h3>
      <div className="p-3">
        {schedule.pending_plan_reason && (
          <p className="mb-2 text-[12.5px] text-muted-foreground">{schedule.pending_plan_reason}</p>
        )}
        <div className="whitespace-pre-wrap rounded-md bg-neutral-900 p-3 font-mono text-[11.5px] leading-relaxed text-neutral-200">
          {schedule.pending_plan_diff.map((line, i) => (
            <DiffLineRow key={i} line={line} />
          ))}
        </div>
        <div className="mt-2.5 flex flex-wrap gap-2">
          <Button variant="outline" size="sm" disabled={busy} onClick={() => run.mutate(true)}>
            Run pending plan (live)
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => update.mutate({ discard_pending: true })}
          >
            Discard
          </Button>
        </div>
        <p className="mt-3 text-xs text-muted-foreground">This one-time live run can create outputs, spend provider budget and deliver externally. Use Validate and approve above to review the change before enabling recurring execution.</p>
        {(run.error || update.error) && <p role="alert" className="mt-2 text-destructive">{(run.error || update.error)?.message}</p>}
      </div>
    </div>
  );
}
