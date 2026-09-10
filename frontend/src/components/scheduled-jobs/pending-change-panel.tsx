"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6, mock state two — "Pending
 * change"). Renders only when `schedule.pending_plan_json` is set (an
 * approved schedule whose instruction was edited since, recompiled into a
 * diff awaiting approval — see `update_schedule`'s docstring,
 * `app/api/v1/schedules.py`). Three actions, each its own mutation:
 * Approve (`POST .../approve` — promotes `pending_plan_json` over
 * `plan_json`, `plan_version += 1`), "Run once with this change"
 * (`POST .../run {use_pending: true}` — previews the pending plan WITHOUT
 * approving it), Discard (`PATCH {discard_pending: true}` — drops the
 * pending plan, leaves the live plan untouched; see this task's own
 * backend addition, `ScheduleUpdate.discard_pending`).
 */

import type { JSX } from "react";
import { Button } from "@/components/ui/button";
import { Pill } from "./shared";
import { useApproveSchedule, useRunSchedule, useUpdateSchedule } from "@/hooks/use-scheduled-jobs";
import type { DiffLine, ScheduleDetail } from "@/hooks/use-scheduled-jobs";

function DiffLineRow({ line }: { line: DiffLine }): JSX.Element {
  const tone = line.kind === "add" ? "text-emerald-400" : line.kind === "del" ? "text-rose-300" : "text-neutral-500";
  return <div className={tone}>{line.text}</div>;
}

export function PendingChangePanel({ schedule }: { schedule: ScheduleDetail }): JSX.Element | null {
  const approve = useApproveSchedule(schedule.id);
  const run = useRunSchedule(schedule.id);
  const update = useUpdateSchedule(schedule.id);

  if (!schedule.pending_plan_json) return null;

  const busy = approve.isPending || run.isPending || update.isPending;

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
          <Button size="sm" disabled={busy} onClick={() => approve.mutate()}>
            Approve · use from next run
          </Button>
          <Button variant="outline" size="sm" disabled={busy} onClick={() => run.mutate(true)}>
            Run once with this change
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
      </div>
    </div>
  );
}
