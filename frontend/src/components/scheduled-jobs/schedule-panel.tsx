"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6, mock state two — "Schedule").
 * Read mode shows the segmented control (Hourly/Daily/Weekly/Monthly/Cron)
 * with the current cadence marked "on", the weekday/time it resolves to,
 * time zone, next run, catch-up policy, and budget. Edit turns the cadence
 * row into buttons plus a weekday/time (or, in Cron mode, a raw cron
 * expression) and a time zone field; Save PATCHes `{cron_expression,
 * timezone}` (`ScheduleUpdate` — every other field on that schema is a
 * different panel's own concern).
 */

import { useState } from "react";
import type { JSX } from "react";
import { Button } from "@/components/ui/button";
import { cronCadence, describeBudget, describeCron, formatCountdown, formatWhen } from "./shared";
import type { CronCadence } from "./shared";
import { useUpdateSchedule } from "@/hooks/use-scheduled-jobs";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

const CADENCES: CronCadence[] = ["hourly", "daily", "weekly", "monthly", "cron"];
const CADENCE_LABEL: Record<CronCadence, string> = {
  hourly: "Hourly",
  daily: "Daily",
  weekly: "Weekly",
  monthly: "Monthly",
  cron: "Cron",
};
const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

interface ParsedCron {
  minute: string;
  hour: string;
  weekday: number;
  monthDay: string;
  raw: string;
}

function parseCron(cron: string | null | undefined): ParsedCron {
  const raw = cron ?? "0 6 * * 1";
  const parts = raw.trim().split(/\s+/);
  if (parts.length !== 5) return { minute: "0", hour: "6", weekday: 1, monthDay: "1", raw };
  const [min, hour, dom, , dow] = parts;
  return {
    minute: /^\d+$/.test(min) ? String(Number(min)) : "0",
    hour: /^\d+$/.test(hour) ? String(Number(hour)) : "6",
    weekday: /^\d+$/.test(dow) ? Number(dow) % 7 : 1,
    monthDay: /^\d+$/.test(dom) ? String(Number(dom)) : "1",
    raw,
  };
}

function buildCronExpression(cadence: CronCadence, p: ParsedCron): string {
  switch (cadence) {
    case "hourly":
      return `${p.minute} * * * *`;
    case "daily":
      return `${p.minute} ${p.hour} * * *`;
    case "weekly":
      return `${p.minute} ${p.hour} * * ${p.weekday}`;
    case "monthly":
      return `${p.minute} ${p.hour} ${p.monthDay} * *`;
    case "cron":
    default:
      return p.raw;
  }
}

const CATCH_UP_COPY: Record<string, string> = {
  once: "if a run is missed (outage), run once when back, never twice",
  skip: "if a run is missed (outage), skip it and wait for the next scheduled time",
};

export function SchedulePanel({ schedule }: { schedule: ScheduleDetail }): JSX.Element {
  const update = useUpdateSchedule(schedule.id);
  const [editing, setEditing] = useState(false);
  const [parsed, setParsed] = useState<ParsedCron>(() => parseCron(schedule.cron_expression));
  const [cadence, setCadence] = useState<CronCadence>(() => cronCadence(schedule.cron_expression));
  const [tz, setTz] = useState(schedule.timezone);

  const currentCadence = cronCadence(schedule.cron_expression);
  const cronText = describeCron(schedule.cron_expression);

  function openEdit() {
    setParsed(parseCron(schedule.cron_expression));
    setCadence(cronCadence(schedule.cron_expression));
    setTz(schedule.timezone);
    setEditing(true);
  }

  function handleTimeChange(value: string) {
    const [h, m] = value.split(":");
    setParsed((p) => ({ ...p, hour: String(Number(h || "0")), minute: String(Number(m || "0")) }));
  }

  function handleSave() {
    update.mutate(
      { cron_expression: buildCronExpression(cadence, parsed), timezone: tz },
      { onSuccess: () => setEditing(false) },
    );
  }

  return (
    <div className="rounded-lg border bg-card">
      <h3 className="flex items-center gap-2 border-b px-3 py-2 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        Schedule
        <span className="flex-1" />
        <Button variant="ghost" size="sm" className="h-6 px-2 text-[11px]" onClick={editing ? handleSave : openEdit}>
          {editing ? "Save" : "Edit"}
        </Button>
        {editing && (
          <Button variant="ghost" size="sm" className="h-6 px-2 text-[11px]" onClick={() => setEditing(false)}>
            Cancel
          </Button>
        )}
      </h3>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3.5 gap-y-1.5 p-3 text-[12.5px]">
        <dt className="pt-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Runs</dt>
        <dd>
          <div className="inline-flex items-center gap-0.5 rounded-md border p-0.5 align-middle">
            {CADENCES.map((c) =>
              editing ? (
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
              ) : (
                <span
                  key={c}
                  className={
                    "rounded px-2 py-0.5 text-[11px] " +
                    (currentCadence === c ? "on bg-accent font-semibold text-accent-foreground" : "text-muted-foreground")
                  }
                >
                  {CADENCE_LABEL[c]}
                </span>
              ),
            )}
          </div>{" "}
          {editing ? (
            <span className="ml-2 inline-flex items-center gap-2 align-middle">
              {cadence === "weekly" && (
                <select
                  aria-label="Weekday"
                  className="h-7 rounded-md border bg-background px-1.5 text-[12px]"
                  value={parsed.weekday}
                  onChange={(e) => setParsed((p) => ({ ...p, weekday: Number(e.target.value) }))}
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
                  value={parsed.monthDay}
                  onChange={(e) => setParsed((p) => ({ ...p, monthDay: e.target.value }))}
                />
              )}
              {(cadence === "daily" || cadence === "weekly" || cadence === "monthly") && (
                <input
                  aria-label="Time"
                  type="time"
                  className="h-7 rounded-md border bg-background px-1.5 text-[12px]"
                  value={`${parsed.hour.padStart(2, "0")}:${parsed.minute.padStart(2, "0")}`}
                  onChange={(e) => handleTimeChange(e.target.value)}
                />
              )}
              {cadence === "cron" && (
                <input
                  aria-label="Cron expression"
                  className="h-7 w-40 rounded-md border bg-background px-1.5 font-mono text-[12px]"
                  value={parsed.raw}
                  onChange={(e) => setParsed((p) => ({ ...p, raw: e.target.value }))}
                />
              )}
            </span>
          ) : (
            <span className="ml-2 align-middle">{cronText.main}</span>
          )}
        </dd>

        <dt className="pt-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
          Time zone
        </dt>
        <dd>
          {editing ? (
            <input
              aria-label="Time zone"
              className="h-7 w-56 rounded-md border bg-background px-1.5 text-[12px]"
              value={tz}
              onChange={(e) => setTz(e.target.value)}
            />
          ) : (
            schedule.timezone
          )}
        </dd>

        <dt className="pt-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
          Next run
        </dt>
        <dd>
          {schedule.next_run_at ? (
            <>
              {formatWhen(schedule.next_run_at)}
              {formatCountdown(schedule.next_run_at) && <> · {formatCountdown(schedule.next_run_at)}</>}
            </>
          ) : (
            "—"
          )}
        </dd>

        <dt className="pt-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
          Catch-up
        </dt>
        <dd>{CATCH_UP_COPY[schedule.catch_up] ?? schedule.catch_up}</dd>

        <dt className="pt-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">Budget</dt>
        <dd>{describeBudget(schedule.budget_json)}</dd>
      </dl>
    </div>
  );
}
