import { it, expect } from "vitest";
import { NAV_ITEMS } from "@/lib/constants";

it("keeps the workflow route and makes it a primary destination", () => {
  const labels = NAV_ITEMS.map((i) => i.label);
  const reportsIdx = labels.indexOf("Reports");
  const settingsIdx = labels.indexOf("Settings");
  const jobsIdx = labels.indexOf("Workflows");
  expect(reportsIdx).toBeGreaterThanOrEqual(0);
  expect(settingsIdx).toBeGreaterThanOrEqual(0);
  expect(jobsIdx).toBeLessThan(reportsIdx);
  expect(jobsIdx).toBeLessThan(settingsIdx);

  const item = NAV_ITEMS.find((i) => i.label === "Workflows")!;
  expect(item.href).toBe("/scheduled-jobs");
  expect(item.icon).toBe("CalendarClock");
  expect(item.featureFlag).toBeNull();
});
