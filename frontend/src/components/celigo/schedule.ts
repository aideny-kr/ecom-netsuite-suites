import type { CeligoSchedule } from "@/hooks/use-celigo-flows";

/** A schedule, humanised for the flow map/table. `"cron"` carries the raw
 * string back (`cron`) alongside the derived facts a caller needs to build a
 * pill or a stall check (`intervalMinutes`), a short label ("every 15 min",
 * "hourly at :10", "3×/day", …) and a compact `display` string (hour lists
 * collapse to "0…23" when the schedule runs every hour). `"on_demand"` is
 * Celigo's `schedule: null` (or an empty string). `"unknown"` is anything
 * that isn't the one six-field shape seen live (see `parseSchedule`'s
 * docstring) -- shown verbatim in `raw`, never guessed at. */
export type ParsedSchedule =
  | { kind: "cron"; cron: string; intervalMinutes: number | null; label: string; display: string }
  | { kind: "on_demand" }
  | { kind: "unknown"; raw: string };

/** Whether a flow's last run is on pace for its own schedule, checked
 * against the SYNC timestamp (`lastSyncedAt`), never the wall clock -- see
 * `stallState`'s docstring for why. `missedRuns`/`intervalMinutes` are only
 * ever present on `"stalled"`/`"on_time"` respectively (both need the
 * schedule to have parsed to a known interval). */
export type StallState = {
  state: "on_time" | "stalled" | "paused" | "on_demand" | "no_run" | "unknown";
  missedRuns?: number;
  intervalMinutes?: number;
};

/** Only the six-field shape `? <minutes> <hours> ? * *` seen live is parsed. Minutes:
 * a comma list or `*\/N`. Hours: a comma list, `*`, `0-23`, or `*\/N`. Anything else
 * is `unknown` and rendered verbatim -- a humaniser that guesses becomes a check that lies. */
function expand(field: string, max: number): number[] | null {
  if (field === "*" || field === `0-${max - 1}`) return Array.from({ length: max }, (_, i) => i);
  const step = /^\*\/(\d+)$/.exec(field);
  if (step) {
    const n = Number(step[1]);
    return n > 0 ? Array.from({ length: Math.ceil(max / n) }, (_, i) => i * n) : null;
  }
  if (!/^\d+(,\d+)*$/.test(field)) return null;
  const list = field.split(",").map(Number).filter((v) => v >= 0 && v < max);
  return list.length ? Array.from(new Set(list)).sort((a, b) => a - b) : null;
}

/** Every circular gap between consecutive values, wrapping the last back to
 * the first. A single value has exactly one gap: the whole period. */
function circularGaps(values: number[], period: number): number[] {
  if (values.length === 1) return [period];
  const gaps = values.slice(1).map((v, i) => v - values[i]);
  gaps.push(period - values[values.length - 1] + values[0]);
  return gaps;
}

function maxGap(values: number[], period: number): number {
  return Math.max(...circularGaps(values, period));
}

/** The trailing three Quartz-style fields (day-of-month, month, day-of-week)
 * on every schedule seen live are `?`, `*`, `*` -- but a plain every-N-hours
 * schedule is sometimes written with day-of-month `*` instead of `?`, since
 * both mean "every day" once day-of-week is also `*`. Accepting either
 * avoids rejecting a real schedule as `unknown` over a Quartz
 * day-of-month/day-of-week convention nobody downstream cares about. */
function hasKnownTail(parts: string[]): boolean {
  if (parts.length !== 6 || parts[0] !== "?" || parts[4] !== "*" || parts[5] !== "*") return false;
  return parts[3] === "?" || parts[3] === "*";
}

export function parseSchedule(schedule: CeligoSchedule): ParsedSchedule {
  if (schedule === null || schedule === "") return { kind: "on_demand" };
  if (typeof schedule !== "string") return { kind: "unknown", raw: JSON.stringify(schedule) };
  const parts = schedule.trim().split(/\s+/);
  if (!hasKnownTail(parts)) return { kind: "unknown", raw: schedule };
  const minutes = expand(parts[1], 60);
  const hours = expand(parts[2], 24);
  if (!minutes || !hours) return { kind: "unknown", raw: schedule };
  const allHours = hours.length === 24;
  let intervalMinutes: number;
  let label: string;
  if (allHours) {
    intervalMinutes = maxGap(minutes, 60);
    label = minutes.length === 1 ? `hourly at :${String(minutes[0]).padStart(2, "0")}` : `every ${intervalMinutes} min`;
  } else if (hours.length === 1 && minutes.length === 1) {
    intervalMinutes = 1440;
    label = `daily ${String(hours[0]).padStart(2, "0")}:${String(minutes[0]).padStart(2, "0")}`;
  } else {
    // The interval is the longest gap between two ACTUAL runs, so it has to
    // be measured over the full minute-of-day set (every hour × every
    // minute), not over the hours alone. Measuring the hour gap ignored the
    // minute list entirely: "? 0,30 0,12" runs at 00:00, 00:30, 12:00 and
    // 12:30, whose longest gap is 11.5 h — it reported 12 h. `stallState`
    // doubles this number to decide "stalled?", so the error went straight
    // into the health pill.
    const minutesOfDay: number[] = [];
    for (const h of hours) for (const m of minutes) minutesOfDay.push(h * 60 + m);
    minutesOfDay.sort((a, b) => a - b);
    const gaps = circularGaps(minutesOfDay, 1440);
    intervalMinutes = Math.max(...gaps);
    // "every N h" is a claim of EVEN spacing on the hour, so it survives only
    // when every gap is the same whole number of hours that divides the day.
    // Anything else is just a count of runs.
    const evenlySpaced = gaps.every((g) => g === gaps[0]);
    const wholeHours = intervalMinutes % 60 === 0 && 1440 % intervalMinutes === 0 && intervalMinutes < 8 * 60;
    label = evenlySpaced && wholeHours ? `every ${intervalMinutes / 60} h` : `${minutesOfDay.length}×/day`;
  }
  const display = `? ${parts[1]} ${allHours ? "0…23" : parts[2]} ? * *`;
  return { kind: "cron", cron: schedule, intervalMinutes, label, display };
}

export function stallState({
  schedule,
  disabled,
  lastExecutedAt,
  lastSyncedAt,
}: {
  schedule: CeligoSchedule;
  disabled: boolean | null;
  lastExecutedAt: string | null;
  lastSyncedAt: string | null;
}): StallState {
  if (disabled === true) return { state: "paused" };
  const parsed = parseSchedule(schedule);
  if (parsed.kind === "on_demand") return { state: "on_demand" };
  if (parsed.kind === "unknown" || parsed.intervalMinutes === null) return { state: "unknown" };
  if (!lastExecutedAt) return { state: "no_run" };
  if (!lastSyncedAt) return { state: "unknown" };
  const ageMin = (Date.parse(lastSyncedAt) - Date.parse(lastExecutedAt)) / 60000;
  if (!Number.isFinite(ageMin)) return { state: "unknown" };
  if (ageMin > 2 * parsed.intervalMinutes) {
    return { state: "stalled", missedRuns: Math.floor(ageMin / parsed.intervalMinutes), intervalMinutes: parsed.intervalMinutes };
  }
  return { state: "on_time", intervalMinutes: parsed.intervalMinutes };
}
