"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6, mock state two — "Compiled
 * plan"). Purely presentational: `schedule.plan_json.steps` in, numbered
 * step rows out. Per-step title/description/kind come from
 * `shared.tsx::describeStep`/`describeStepParams` — a client-side mirror of
 * `app.services.jobs.registry.STEP_REGISTRY`, since `ScheduleDetailResponse`
 * carries only the raw `{id, type, params}` per step (the API's own
 * per-step label lookup, `_plan_kinds`/`_plan_summary_line` in
 * `app/api/v1/schedules.py`, is list-level/aggregate only).
 *
 * The "guard" line the mock shows per step (bytes scanned, budget checks,
 * idempotency key text) is run-time telemetry this API never carries at
 * compile time — rather than fabricate numbers, this panel shows only the
 * READ/WRITE tag plus, for a write step, the "allow-listed" pill the mock
 * also uses (spec §B6's own honest-simplification precedent — see
 * `jobs-list.tsx`'s file docstring for the two documented list-page cases).
 */

import type { JSX } from "react";
import { Pill } from "./shared";
import { describeStep, describeStepParams } from "./shared";
import type { PlanStep, ScheduleDetail } from "@/hooks/use-scheduled-jobs";

const AGENT_NOTE =
  "steps come only from the job registry (queries, reports, files, Drive, email, recon runs). A NetSuite or Celigo write cannot appear in a scheduled plan today; when it can, it will require the same confirmation card the chat uses, on a human's screen, before the job may include it.";

function StepRow({ step, index }: { step: PlanStep; index: number }): JSX.Element {
  const meta = describeStep(step.type);
  const description = describeStepParams(step.type, step.params ?? {});
  return (
    <div className="grid grid-cols-[22px_1fr_auto] items-start gap-2.5 border-b py-2 last:border-0">
      <span className="flex h-[22px] w-[22px] items-center justify-center rounded-full border text-[11px] font-bold text-muted-foreground">
        {index + 1}
      </span>
      <div className="min-w-0">
        <div className="text-[13px] font-semibold">{meta.label}</div>
        {description && (
          <div className="mt-0.5 truncate font-mono text-[11.5px] text-muted-foreground" title={description}>
            {description}
          </div>
        )}
      </div>
      <div className="flex flex-col items-end gap-1">
        <span
          className={
            meta.kind === "write"
              ? "rounded bg-violet-500/10 px-1 text-[10px] font-bold uppercase tracking-wide text-violet-700 dark:text-violet-400"
              : "rounded bg-blue-500/10 px-1 text-[10px] font-bold uppercase tracking-wide text-blue-700 dark:text-blue-400"
          }
        >
          {meta.kind.toUpperCase()}
        </span>
        {meta.kind === "write" && <Pill tone="mute">allow-listed</Pill>}
      </div>
    </div>
  );
}

export function PlanPanel({ schedule }: { schedule: ScheduleDetail }): JSX.Element {
  const steps = schedule.plan_json?.steps ?? [];
  return (
    <div className="rounded-lg border bg-card">
      <h3 className="flex items-center gap-2 border-b px-3 py-2 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        Compiled plan
        <span className="flex-1" />
        {steps.length > 0 && (
          <Pill tone="mute">
            v{schedule.plan_version} · {schedule.plan_status ?? "draft"}
          </Pill>
        )}
      </h3>
      <div className="p-3">
        {steps.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No compiled plan yet.</p>
        ) : (
          <div className="flex flex-col">
            {steps.map((step, i) => (
              <StepRow key={step.id} step={step} index={i} />
            ))}
          </div>
        )}
      </div>
      <div className="border-t px-3 py-2.5 text-[12px] text-muted-foreground">
        <b className="text-foreground">What the agent may not do here:</b> {AGENT_NOTE}
      </div>
    </div>
  );
}
