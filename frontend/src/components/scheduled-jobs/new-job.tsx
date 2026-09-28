"use client";

/** Compile through the real schedule API, set cadence and limits, then review
 * the persisted plan on its durable detail route before approval. */

import { useState } from "react";
import type { JSX } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import { useCreateSchedule, useScheduledJob, useUpdateSchedule } from "@/hooks/use-scheduled-jobs";
import type { PlanStep } from "@/hooks/use-scheduled-jobs";
import { describeStep, describeStepParams, Pill } from "./shared";

const HINT =
  "Plain language. Name the source, the output, where it should go, and what to do when something is off. The agent asks if something is missing before it compiles.";

/** Same convention as `instruction-panel.tsx::parseClarification` — the
 * generic `ApiError` message is `JSON.stringify(detail)` for an object
 * `detail`, so a 409's `.message` here is the literal `{"clarification":
 * "..."}` text, not the question. Parsed defensively: any other 409 (or any
 * other status) falls through to the plain-error path instead of a blank
 * clarification banner. */
function parseClarification(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  try {
    const parsed = JSON.parse(err.message);
    return typeof parsed?.clarification === "string" ? parsed.clarification : null;
  } catch {
    return null;
  }
}

type Cadence = "hourly" | "daily" | "weekly" | "monthly" | "cron";
const CADENCES: Cadence[] = ["hourly", "daily", "weekly", "monthly", "cron"];
const CADENCE_LABEL: Record<Cadence, string> = {
  hourly: "Hourly",
  daily: "Daily",
  weekly: "Weekly",
  monthly: "Monthly",
  cron: "Cron",
};
const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function buildCron(
  cadence: Cadence,
  opts: { hour: string; minute: string; weekday: number; monthDay: string; raw: string },
): string {
  switch (cadence) {
    case "hourly":
      return `${opts.minute} * * * *`;
    case "daily":
      return `${opts.minute} ${opts.hour} * * *`;
    case "weekly":
      return `${opts.minute} ${opts.hour} * * ${opts.weekday}`;
    case "monthly":
      return `${opts.minute} ${opts.hour} ${opts.monthDay} * *`;
    case "cron":
    default:
      return opts.raw;
  }
}

function localTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

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
      <span
        className={
          meta.kind === "write"
            ? "rounded bg-violet-500/10 px-1 text-[10px] font-bold uppercase tracking-wide text-violet-700 dark:text-violet-400"
            : "rounded bg-blue-500/10 px-1 text-[10px] font-bold uppercase tracking-wide text-blue-700 dark:text-blue-400"
        }
      >
        {meta.kind.toUpperCase()}
      </span>
    </div>
  );
}

export function NewJob(): JSX.Element {
  const router = useRouter();
  const create = useCreateSchedule();

  const [instruction, setInstruction] = useState("");
  const [answer, setAnswer] = useState("");
  const [clarification, setClarification] = useState<string | null>(null);
  const [createdId, setCreatedId] = useState<string | null>(null);

  const [cadence, setCadence] = useState<Cadence>("weekly");
  const [hour, setHour] = useState("9");
  const [minute, setMinute] = useState("0");
  const [weekday, setWeekday] = useState(1);
  const [monthDay, setMonthDay] = useState("1");
  const [rawCron, setRawCron] = useState("0 9 * * 1");
  const [timezone, setTimezone] = useState<string>(localTimeZone);
  const [seconds, setSeconds] = useState("");
  const [bytes, setBytes] = useState("");
  const [usd, setUsd] = useState("");

  const detailQuery = useScheduledJob(createdId ?? "");
  const update = useUpdateSchedule(createdId ?? "");

  const step: "compose" | "review" = createdId || clarification ? "review" : "compose";

  function submit(text: string) {
    create.mutate(
      { instruction: text },
      {
        onSuccess: (data: { id: string }) => {
          setCreatedId(data.id);
          setClarification(null);
          setAnswer("");
        },
        onError: (err: unknown) => {
          setClarification(parseClarification(err));
        },
      },
    );
  }

  function handleCompile() {
    submit(instruction);
  }

  function handleRecompile() {
    const combined = answer.trim() ? `${instruction}\n\n${answer.trim()}` : instruction;
    setInstruction(combined);
    submit(combined);
  }

  function handleBackToInstruction() {
    setClarification(null);
    setCreatedId(null);
  }

  function handleSave() {
    if (!createdId) return;
    if (!limitsValid) return;
    update.mutate(
      {
        cron_expression: buildCron(cadence, { hour, minute, weekday, monthDay, raw: rawCron }),
        timezone,
        budget: Object.fromEntries([["seconds", seconds], ...(hasStandaloneQuery ? [["bytes_scanned", bytes], ...(!hasAgent ? [["usd", usd]] : [])] : [])].filter(([, v]) => v !== "").map(([k, v]) => [k, Number(v)])),
      },
      { onSuccess: () => router.push(`/scheduled-jobs/${createdId}`) },
    );
  }

  const plainError = create.isError && !clarification ? ((create.error as Error | null)?.message ?? null) : null;
  const steps = detailQuery.data?.plan_json?.steps ?? [];
  const hasStandaloneQuery = steps.some((step) => step.type === "bigquery_sql");
  const hasAgent = steps.some((step) => step.type === "agent.review_saved_case");
  const limitsValid = [seconds, ...(hasStandaloneQuery ? [bytes, ...(!hasAgent ? [usd] : [])] : [])].every((v) => v === "" || (Number.isFinite(Number(v)) && Number(v) > 0));

  return (
    <div className="max-w-2xl animate-fade-in space-y-4">
      <h2 className="text-2xl font-semibold tracking-tight">New job</h2>

      {step === "compose" && (
        <div className="rounded-lg border bg-card">
          <h3 className="border-b px-3 py-2 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
            New job · 1 of 2 · what should it do?
          </h3>
          <div className="space-y-2 p-3">
            <textarea
              className="min-h-[96px] w-full rounded-md border bg-background p-2.5 text-[14px] leading-relaxed"
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              disabled={create.isPending}
              aria-label="Workflow instruction"
              maxLength={4000}
              placeholder="Every Monday, compose the inventory aging report for the selected stock locations and save PDF and Excel outputs."
            />
            <p className="text-[11.5px] text-muted-foreground">{HINT}</p>
            {plainError && <p className="text-[12px] text-destructive">{plainError}</p>}
            <div className="flex gap-2">
              <Button size="sm" disabled={create.isPending || !instruction.trim()} onClick={handleCompile}>
                Compile plan →
              </Button>
              <Button asChild variant="ghost" size="sm">
                <Link href="/scheduled-jobs">Cancel</Link>
              </Button>
            </div>
          </div>
        </div>
      )}

      {step === "review" && (
        <div className="rounded-lg border bg-card">
          <h3 className="border-b px-3 py-2 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
            New job · 2 of 2 · review the plan, then schedule
          </h3>
          <div className="space-y-3 p-3">
            {clarification ? (
              <div className="space-y-2">
                <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-2 text-[12.5px]">
                  <b>Missing:</b> the agent asks one question before saving — {clarification}
                </div>
                <textarea
                  className="min-h-[72px] w-full rounded-md border bg-background p-2.5 text-[14px] leading-relaxed"
                  value={answer}
                  onChange={(e) => setAnswer(e.target.value)}
                  disabled={create.isPending}
                  placeholder="Answer the agent's question…"
                />
                <div className="flex gap-2">
                  <Button size="sm" disabled={create.isPending || !answer.trim()} onClick={handleRecompile}>
                    Compile plan →
                  </Button>
                  <Button variant="ghost" size="sm" onClick={handleBackToInstruction} disabled={create.isPending}>
                    Back
                  </Button>
                </div>
              </div>
            ) : (
              <>
                {detailQuery.isPending ? (
                  <p className="text-[13px] text-muted-foreground">Loading the compiled plan…</p>
                ) : (
                  <div className="flex flex-col">
                    {steps.map((s, i) => (
                      <StepRow key={s.id} step={s} index={i} />
                    ))}
                  </div>
                )}

                <div className="space-y-2 border-t pt-3">
                  <div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Schedule
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="inline-flex items-center gap-0.5 rounded-md border p-0.5">
                      {CADENCES.map((c) => (
                        <button
                          key={c}
                          type="button"
                          onClick={() => setCadence(c)}
                          className={
                            "rounded px-2 py-0.5 text-[11px] " +
                            (cadence === c ? "bg-accent font-semibold text-accent-foreground" : "text-muted-foreground")
                          }
                        >
                          {CADENCE_LABEL[c]}
                        </button>
                      ))}
                    </div>
                    {cadence === "weekly" && (
                      <select
                        aria-label="Weekday"
                        className="h-7 rounded-md border bg-background px-1.5 text-[12px]"
                        value={weekday}
                        onChange={(e) => setWeekday(Number(e.target.value))}
                      >
                        {WEEKDAYS.map((w, i) => (
                          <option key={w} value={i}>
                            {w}
                          </option>
                        ))}
                      </select>
                    )}
                    {cadence === "monthly" && (
                      <input
                        aria-label="Day of month"
                        type="number"
                        min={1}
                        max={28}
                        className="h-7 w-16 rounded-md border bg-background px-1.5 text-[12px]"
                        value={monthDay}
                        onChange={(e) => setMonthDay(e.target.value)}
                      />
                    )}
                    {(cadence === "daily" || cadence === "weekly" || cadence === "monthly") && (
                      <input
                        aria-label="Time"
                        type="time"
                        className="h-7 rounded-md border bg-background px-1.5 text-[12px]"
                        value={`${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`}
                        onChange={(e) => {
                          const [h, m] = e.target.value.split(":");
                          setHour(String(Number(h || "0")));
                          setMinute(String(Number(m || "0")));
                        }}
                      />
                    )}
                    {cadence === "cron" && (
                      <input
                        aria-label="Cron expression"
                        className="h-7 w-40 rounded-md border bg-background px-1.5 font-mono text-[12px]"
                        value={rawCron}
                        onChange={(e) => setRawCron(e.target.value)}
                      />
                    )}
                    <input
                      aria-label="Time zone"
                      className="h-7 w-56 rounded-md border bg-background px-1.5 text-[12px]"
                      value={timezone}
                      onChange={(e) => setTimezone(e.target.value)}
                    />
                  </div>

                  <div className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Delivery
                  </div>
                  <p className="text-[13px] text-muted-foreground">Delivery follows the compiled steps. Reports stay in app; Drive uploads use the connected Drive’s Reports folder and the report’s folder. Email and arbitrary folder overrides are not supported.</p>
                  <fieldset className="space-y-2">
                    <legend className="text-[13px] font-semibold">Per-run limits</legend>
                    {[["Maximum seconds", seconds, setSeconds], ...(hasStandaloneQuery ? [["Maximum bytes scanned", bytes, setBytes], ...(!hasAgent ? [["Maximum query cost (USD)", usd, setUsd]] : [])] : [])].map(([label, value, setter]) => (
                      <label key={label as string} className="flex flex-wrap items-center gap-2 text-[13px]">
                        {label as string}<input aria-label={label as string} type="number" min="0.01" step="any" className="h-8 w-40 rounded-md border bg-background px-2" value={value as string} onChange={(e) => (setter as (v: string) => void)(e.target.value)} />
                      </label>
                    ))}
                    <p className="text-xs text-muted-foreground">Blank means no workflow limit. Bytes and estimated query cost cover standalone bigquery_sql steps only; internal report queries are not metered by these limits. Agent steps use their own token and time limits and do not support a USD ceiling. A time limit can interrupt a Drive upload with an unknown outcome that requires reconciliation.</p>
                  </fieldset>
                </div>

                {update.error && <p role="alert" className="text-destructive">{update.error.message}</p>}
                {detailQuery.isError && <p role="alert" className="text-destructive">The compiled plan could not be loaded. Reload this workflow before saving.</p>}
                <div className="flex items-center gap-2">
                  <Button size="sm" disabled={update.isPending || detailQuery.isPending || detailQuery.isError || !steps.length || !limitsValid} onClick={handleSave}>
                    Save
                  </Button>
                  <Button asChild variant="ghost" size="sm">
                    <Link href="/scheduled-jobs">Cancel</Link>
                  </Button>
                  <span className="flex-1" />
                  <Pill tone="warn">pending_approval</Pill>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
