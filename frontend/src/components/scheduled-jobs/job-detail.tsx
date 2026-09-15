"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6, mock state two — "a job").
 * The full detail page: head actions (Run now, Pause/Resume, Duplicate,
 * Delete), then the instruction, compiled plan, pending change (only when
 * present), schedule, delivery, and run history panels — each its own file,
 * each covered by its own test; this component owns fetching the schedule,
 * the head actions, and composition.
 *
 * "Duplicate" (mock state two's head row) has no backing endpoint in this
 * slice — Tasks 1-5's interfaces carry no `POST /schedules/{id}/duplicate`
 * and no New Job flow exists yet to hand a prefilled instruction to (owned
 * by a later task, per the brief). Rendered disabled rather than omitted,
 * so the mock's copy still appears, honestly non-functional until that
 * lands — the running list of a schedule's file-cabinet-adjacent gaps.
 */

import { useState } from "react";
import type { JSX } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { DeliveryPanel } from "./delivery-panel";
import { InstructionPanel } from "./instruction-panel";
import { PendingChangePanel } from "./pending-change-panel";
import { PlanPanel } from "./plan-panel";
import { RunsPanel } from "./runs-panel";
import { SchedulePanel } from "./schedule-panel";
import { ErrorNotice, Pill } from "./shared";
import type { PillTone } from "./shared";
import {
  useDeleteSchedule,
  usePauseSchedule,
  useResumeScheduledJob,
  useRunSchedule,
  useScheduledJob,
} from "@/hooks/use-scheduled-jobs";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

function statusPill(schedule: ScheduleDetail): { label: string; tone: PillTone } {
  if (schedule.paused_at) return { label: "paused", tone: "warn" };
  if (schedule.plan_status === "approved") return { label: "active", tone: "ok" };
  if (schedule.plan_status === "pending_approval") return { label: "awaiting approval", tone: "warn" };
  return { label: schedule.plan_status ?? "draft", tone: "mute" };
}

function DeleteJobDialog({
  open,
  onOpenChange,
  jobName,
  onConfirm,
  isPending,
  error,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  jobName: string;
  onConfirm: () => void;
  isPending: boolean;
  error: Error | null;
}): JSX.Element {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete this job?</AlertDialogTitle>
          <AlertDialogDescription>
            {jobName} will be permanently removed, along with its schedule and delivery settings. Its run history
            stays in the audit log. This can&apos;t be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error && (
          <p role="alert" className="text-[13px] text-destructive">
            {error.message}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={isPending}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={(e) => {
              e.preventDefault();
              onConfirm();
            }}
            disabled={isPending}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            Delete job
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export function JobDetail({ id }: { id: string }): JSX.Element {
  const router = useRouter();
  const scheduleQuery = useScheduledJob(id);
  const run = useRunSchedule(id);
  const pause = usePauseSchedule(id);
  const resume = useResumeScheduledJob();
  const del = useDeleteSchedule(id);
  const [deleteOpen, setDeleteOpen] = useState(false);

  if (scheduleQuery.isPending) {
    return (
      <div className="space-y-3" aria-busy="true">
        <span className="sr-only">Loading job…</span>
        <Skeleton className="h-8 w-64 rounded-lg" />
        <Skeleton className="h-40 w-full rounded-lg" />
      </div>
    );
  }
  if (scheduleQuery.isError || !scheduleQuery.data) {
    return <ErrorNotice message="Couldn't load this job." onRetry={() => scheduleQuery.refetch()} />;
  }

  const schedule = scheduleQuery.data;
  const pill = statusPill(schedule);
  const approved = schedule.plan_status === "approved" && !schedule.paused_at;

  return (
    <div className="space-y-4 animate-fade-in">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <Link href="/scheduled-jobs" className="text-[12.5px] text-muted-foreground hover:underline">
            Scheduled jobs ›
          </Link>
          <div className="flex items-center gap-2">
            <h2 className="text-2xl font-semibold tracking-tight">{schedule.name}</h2>
            <Pill tone={pill.tone}>{pill.label}</Pill>
          </div>
        </div>
        <div className="ml-auto flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={run.isPending || !approved}
            title={approved ? undefined : "Approve the compiled plan before running it"}
            onClick={() => run.mutate(false)}
          >
            Run now
          </Button>
          {schedule.paused_at ? (
            <Button variant="outline" size="sm" disabled={resume.isPending} onClick={() => resume.mutate(schedule.id)}>
              Resume
            </Button>
          ) : (
            <Button variant="outline" size="sm" disabled={pause.isPending} onClick={() => pause.mutate()}>
              Pause
            </Button>
          )}
          <Button variant="outline" size="sm" disabled title="Not available yet">
            Duplicate
          </Button>
          <Button variant="outline" size="sm" className="text-destructive" onClick={() => setDeleteOpen(true)}>
            Delete
          </Button>
        </div>
      </div>

      {schedule.paused_at && schedule.pause_reason && (
        <ErrorNotice message={`Paused: ${schedule.pause_reason}`} />
      )}

      <div className="grid grid-cols-1 gap-3.5 lg:grid-cols-[1.35fr_1fr]">
        <div className="flex min-w-0 flex-col gap-3.5">
          <InstructionPanel schedule={schedule} />
          <PlanPanel schedule={schedule} />
          <PendingChangePanel schedule={schedule} />
        </div>
        <div className="flex min-w-0 flex-col gap-3.5">
          <SchedulePanel schedule={schedule} />
          <DeliveryPanel schedule={schedule} />
          <RunsPanel scheduleId={schedule.id} />
        </div>
      </div>

      <DeleteJobDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        jobName={schedule.name}
        isPending={del.isPending}
        error={del.error ?? null}
        onConfirm={() =>
          del.mutate(undefined, {
            onSuccess: () => router.push("/scheduled-jobs"),
          })
        }
      />
    </div>
  );
}
