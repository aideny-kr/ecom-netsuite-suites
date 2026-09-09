import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import {
  KindTags,
  Pill,
  describeCron,
  describeDelivery,
  formatCountdown,
  formatWhen,
  runStatusLabel,
  runStatusTone,
} from "@/components/scheduled-jobs/shared";

// --- Pill / KindTags ---------------------------------------------------------

it("Pill renders its tone and children", () => {
  render(<Pill tone="ok">done</Pill>);
  expect(screen.getByText("done")).toBeInTheDocument();
});

it("KindTags renders READ and WRITE tags for a plan with both kinds", () => {
  render(<KindTags kinds={["read", "write"]} />);
  expect(screen.getByText("READ")).toBeInTheDocument();
  expect(screen.getByText("WRITE")).toBeInTheDocument();
});

it("KindTags renders only READ when the plan has no write step", () => {
  render(<KindTags kinds={["read"]} />);
  expect(screen.getByText("READ")).toBeInTheDocument();
  expect(screen.queryByText("WRITE")).toBeNull();
});

// --- describeCron -------------------------------------------------------------

it("describeCron reads a weekly cron as 'Weekly · Mon 06:00' with the raw cron as the sub-line", () => {
  const { main, sub } = describeCron("0 6 * * 1");
  expect(main).toBe("Weekly · Mon 06:00");
  expect(sub).toBe("cron 0 6 * * 1");
});

it("describeCron reads a daily cron", () => {
  expect(describeCron("0 7 * * *").main).toBe("Daily · 07:00");
});

it("describeCron reads an hourly cron", () => {
  expect(describeCron("0 * * * *").main).toBe("Hourly · :00");
});

it("describeCron falls back to a plain label for a shape it doesn't recognise, keeping the raw cron visible", () => {
  const { main, sub } = describeCron("*/15 * * * *");
  expect(main).toBe("Cron");
  expect(sub).toBe("cron */15 * * * *");
});

it("describeCron returns an em dash for no cron at all", () => {
  expect(describeCron(null).main).toBe("—");
});

// --- describeDelivery -----------------------------------------------------------

it("describeDelivery renders a Drive delivery with its folder", () => {
  const { label, sub } = describeDelivery({ drive: { folder: "Reports / Inventory aging" } });
  expect(label).toBe("Drive");
  expect(sub).toBe("Reports / Inventory aging");
});

it("describeDelivery renders an email delivery with its recipients", () => {
  const { label, sub } = describeDelivery({ email: { to: "ops@", count: 2 } });
  expect(label).toBe("Email");
  expect(sub).toContain("ops@");
  expect(sub).toContain("2");
});

it("describeDelivery falls back to an em dash for no delivery data, never a guess", () => {
  expect(describeDelivery(null).label).toBe("—");
  expect(describeDelivery({}).label).toBe("—");
});

// --- run status ------------------------------------------------------------------

it("runStatusTone maps 'done' to ok and 'error' to crit", () => {
  expect(runStatusTone("done")).toBe("ok");
  expect(runStatusTone("error")).toBe("crit");
  expect(runStatusTone("stall")).toBe("crit");
  expect(runStatusTone("blocked")).toBe("crit");
  expect(runStatusTone("budget")).toBe("warn");
});

it("runStatusLabel says 'never run' when there is no last_run_status yet, never fabricating a status", () => {
  expect(runStatusLabel(null)).toBe("never run");
});

it("runStatusLabel translates the retry_pending internal value to a readable label", () => {
  expect(runStatusLabel("retry_pending")).toBe("retrying");
});

// --- formatWhen / formatCountdown ------------------------------------------------

it("formatWhen returns an em dash for a null timestamp", () => {
  expect(formatWhen(null)).toBe("—");
});

it("formatWhen formats a real timestamp with a weekday", () => {
  const out = formatWhen("2026-09-08T13:00:00Z");
  expect(out).toMatch(/Sep/);
});

it("formatCountdown returns null when there is no next run", () => {
  expect(formatCountdown(null)).toBeNull();
});

it("formatCountdown counts forward in days and hours from a fixed 'now'", () => {
  const now = new Date("2026-09-09T00:00:00Z");
  const out = formatCountdown("2026-09-14T06:00:00Z", now);
  expect(out).toBe("in 5 d 6 h");
});

it("formatCountdown says 'due now' for a run time already in the past", () => {
  const now = new Date("2026-09-09T00:00:00Z");
  expect(formatCountdown("2026-09-01T00:00:00Z", now)).toBe("due now");
});
