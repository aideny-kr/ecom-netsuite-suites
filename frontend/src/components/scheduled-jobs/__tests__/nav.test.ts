import { it, expect } from "vitest";
import { NAV_ITEMS } from "@/lib/constants";

it("adds a Scheduled jobs nav item between Reports and Settings", () => {
  const labels = NAV_ITEMS.map((i) => i.label);
  const reportsIdx = labels.indexOf("Reports");
  const settingsIdx = labels.indexOf("Settings");
  const jobsIdx = labels.indexOf("Scheduled jobs");
  expect(reportsIdx).toBeGreaterThanOrEqual(0);
  expect(settingsIdx).toBeGreaterThanOrEqual(0);
  expect(jobsIdx).toBe(reportsIdx + 1);
  expect(jobsIdx).toBe(settingsIdx - 1);

  const item = NAV_ITEMS.find((i) => i.label === "Scheduled jobs")!;
  expect(item.href).toBe("/scheduled-jobs");
  expect(item.icon).toBe("CalendarClock");
  expect(item.featureFlag).toBeNull();
});
