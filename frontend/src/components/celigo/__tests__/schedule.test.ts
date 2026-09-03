import { describe, expect, it } from "vitest";
import { parseSchedule, stallState } from "../schedule";

const HOURS = "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23";

describe("parseSchedule — only the shapes seen live are humanised", () => {
  it("minute list over every hour", () => {
    const p = parseSchedule(`? 5,20,35,50 ${HOURS} ? * *`);
    expect(p).toMatchObject({ kind: "cron", intervalMinutes: 15, label: "every 15 min", display: "? 5,20,35,50 0…23 ? * *" });
  });
  it("two minutes an hour, single minute an hour, hour lists, */N", () => {
    expect(parseSchedule(`? 5,35 ${HOURS} ? * *`)).toMatchObject({ intervalMinutes: 30, label: "every 30 min" });
    expect(parseSchedule(`? 10 ${HOURS} ? * *`)).toMatchObject({ intervalMinutes: 60, label: "hourly at :10" });
    expect(parseSchedule("? 5 0,4,8,12,16,20 ? * *")).toMatchObject({ intervalMinutes: 240, label: "every 4 h" });
    expect(parseSchedule("? 5 2,10,18 ? * *")).toMatchObject({ intervalMinutes: 480, label: "3×/day" });
    expect(parseSchedule("? 5 6 ? * *")).toMatchObject({ intervalMinutes: 1440, label: "daily 06:05" });
    expect(parseSchedule("? 0 */6 * * *")).toMatchObject({ intervalMinutes: 360, label: "every 6 h" });
  });
  // Codex fix wave, item 12. On an hour SUBSET the interval was computed from
  // the HOUR gap alone, so every minute in the list was ignored: "? 0,30 0,12"
  // (four runs a day, at 00:00, 00:30, 12:00, 12:30) reported a 12-hour
  // interval when the real longest gap between runs is 11.5 hours — and
  // `stallState` multiplies that interval by two to decide "stalled?", so the
  // error propagated straight into the health pill.
  it("item 12: minutes count on an hour subset — the interval is the longest real gap", () => {
    // minute-of-day set {0, 30, 720, 750}; gaps 30 · 690 · 30 · 690 → 690.
    expect(parseSchedule("? 0,30 0,12 ? * *")).toMatchObject({ intervalMinutes: 690, label: "4×/day" });
  });

  it("item 12: an evenly-spaced hourly set still reads 'every N h'", () => {
    expect(parseSchedule("? 0 0,6,12,18 ? * *")).toMatchObject({ intervalMinutes: 360, label: "every 6 h" });
    // Two minutes an hour on a 6-hourly set is no longer "every 6 h": the
    // runs are 30 minutes apart, then 5.5 hours apart.
    expect(parseSchedule("? 0,30 0,6,12,18 ? * *")).toMatchObject({ intervalMinutes: 330, label: "8×/day" });
  });

  it("null, empty string and JSON null are on demand", () => {
    expect(parseSchedule(null)).toEqual({ kind: "on_demand" });
    expect(parseSchedule("")).toEqual({ kind: "on_demand" });
  });
  it("anything else is unknown and shown verbatim", () => {
    expect(parseSchedule("? 0-59/15 * ? * *")).toEqual({ kind: "unknown", raw: "? 0-59/15 * ? * *" });
    expect(parseSchedule({ type: "everyN" })).toMatchObject({ kind: "unknown" });
    expect(parseSchedule(false)).toMatchObject({ kind: "unknown" });
  });
});

describe("stallState — against the sync time, never the wall clock", () => {
  const cron15 = `? 5,20,35,50 ${HOURS} ? * *`;
  const sync = "2026-09-02T18:12:00Z";
  it("21 minutes before the sync on a 15-minute schedule is on time", () => {
    expect(stallState({ schedule: cron15, disabled: false, lastExecutedAt: "2026-09-02T17:51:00Z", lastSyncedAt: sync })).toEqual({ state: "on_time", intervalMinutes: 15 });
  });
  it("3 hours before the sync is stalled? with 12 runs missed", () => {
    expect(stallState({ schedule: cron15, disabled: false, lastExecutedAt: "2026-09-02T15:12:00Z", lastSyncedAt: sync })).toEqual({ state: "stalled", missedRuns: 12, intervalMinutes: 15 });
  });
  it("paused is never stalled; on demand and unknown crons make no claim; no run is its own state", () => {
    expect(stallState({ schedule: cron15, disabled: true, lastExecutedAt: "2024-04-15T00:00:00Z", lastSyncedAt: sync }).state).toBe("paused");
    expect(stallState({ schedule: null, disabled: false, lastExecutedAt: "2026-01-01T00:00:00Z", lastSyncedAt: sync }).state).toBe("on_demand");
    expect(stallState({ schedule: "? 0-59/15 * ? * *", disabled: false, lastExecutedAt: "2026-01-01T00:00:00Z", lastSyncedAt: sync }).state).toBe("unknown");
    expect(stallState({ schedule: cron15, disabled: false, lastExecutedAt: null, lastSyncedAt: sync }).state).toBe("no_run");
    expect(stallState({ schedule: cron15, disabled: false, lastExecutedAt: "2026-09-02T15:12:00Z", lastSyncedAt: null }).state).toBe("unknown");
  });
});
