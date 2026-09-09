"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6) — small presentational and
 * pure-formatting pieces shared by the list page (and, later, the job
 * detail / new-job pages): tags (READ/WRITE), pills (run status / system),
 * and tiles, plus the cron/delivery/time formatters the table cells need.
 * Kept dependency-free of any one query hook so every piece here is a pure
 * function of the data it's handed — easy to unit test, and reusable by a
 * later detail page without dragging the list page's hooks along.
 */

import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import type { DeliveryJson } from "@/hooks/use-scheduled-jobs";

// ---------------------------------------------------------------------------
// ErrorNotice — mirrors components/celigo/shared.tsx's own ErrorNotice
// (not imported from there to keep this surface's shared module
// self-contained, per this task's file ownership).
// ---------------------------------------------------------------------------

export function ErrorNotice({ message, onRetry }: { message: string; onRetry?: () => void }): JSX.Element {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/5 px-3 py-2 text-[13px] text-destructive">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      <span className="flex-1">{message}</span>
      {onRetry && (
        <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pill — one status-chip primitive, mirrors the tone system already in use
// on the Celigo pages (components/celigo/shared.tsx) so status chips read
// consistently across the app.
// ---------------------------------------------------------------------------

export type PillTone = "ok" | "warn" | "crit" | "mute";

const PILL_TONE_CLASSES: Record<PillTone, string> = {
  ok: "border-green-500/50 bg-green-500/10 text-green-700 dark:text-green-400",
  warn: "border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  crit: "border-red-500/50 bg-red-500/10 text-red-700 dark:text-red-400",
  mute: "border-border bg-muted text-muted-foreground",
};

export function Pill({ tone, children }: { tone: PillTone; children: ReactNode }): JSX.Element {
  return (
    <span
      className={cn(
        "inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[10.5px] font-semibold leading-tight",
        PILL_TONE_CLASSES[tone],
      )}
    >
      {children}
    </span>
  );
}

/** The "system" pill for a Beat-config row (mock: "Report auto-refresh") —
 * visually distinct from a run-status Pill (outline only, no fill) so it
 * reads as a category label, not an outcome. */
export function SystemPill(): JSX.Element {
  return (
    <span className="inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[10.5px] font-semibold leading-tight text-muted-foreground">
      system
    </span>
  );
}

// ---------------------------------------------------------------------------
// KindTags — the mock's READ/WRITE tags, driven by `ScheduleResponse.kinds`
// (derived server-side from the compiled plan's step registry lookups).
// ---------------------------------------------------------------------------

export function KindTags({ kinds }: { kinds: string[] }): JSX.Element {
  return (
    <span className="inline-flex gap-1">
      {kinds.includes("read") && (
        <span className="rounded bg-blue-500/10 px-1 text-[10px] font-bold uppercase tracking-wide text-blue-700 dark:text-blue-400">
          READ
        </span>
      )}
      {kinds.includes("write") && (
        <span className="rounded bg-violet-500/10 px-1 text-[10px] font-bold uppercase tracking-wide text-violet-700 dark:text-violet-400">
          WRITE
        </span>
      )}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Tile — the four list-page tiles (Jobs, Last 7 days, Needs attention, Next
// run), styled to the mock's left-border-accent tile.
// ---------------------------------------------------------------------------

export function Tile({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "ok" | "warn";
}): JSX.Element {
  return (
    <div
      className={cn(
        "rounded-lg border border-l-2 bg-card p-3 shadow-soft",
        tone === "warn" ? "border-l-amber-500" : tone === "ok" ? "border-l-green-500" : "border-l-border",
      )}
    >
      <dt className="text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-[19px] leading-tight tabular-nums">{value}</dd>
      {sub != null && <div className="mt-0.5 text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// describeCron — a 5-field cron (`min hour dom month dow`, evaluated in the
// schedule's own timezone) into the mock's "Weekly · Mon 06:00" style main
// line plus the raw cron as the sub-line. Recognises exactly the shapes the
// New Job flow's segmented control offers (Hourly/Daily/Weekly/Monthly);
// anything else falls back to a plain "Cron" label with the raw expression
// still visible, rather than a guess.
// ---------------------------------------------------------------------------

const WEEKDAY_ABBR = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function pad2(v: string): string {
  return v.padStart(2, "0");
}

export function describeCron(cron: string | null | undefined): { main: string; sub: string | null } {
  if (!cron) return { main: "—", sub: null };
  const parts = cron.trim().split(/\s+/);
  const sub = `cron ${cron}`;
  if (parts.length !== 5) return { main: "Cron", sub };
  const [min, hour, dom, month, dow] = parts;
  const isFixedMinute = /^\d+$/.test(min);
  const isFixedHour = /^\d+$/.test(hour);
  const isFixedDom = /^\d+$/.test(dom);
  const isFixedDow = /^\d+$/.test(dow);

  if (isFixedMinute && hour === "*" && dom === "*" && month === "*" && dow === "*") {
    return { main: `Hourly · :${pad2(min)}`, sub };
  }
  if (isFixedMinute && isFixedHour && dom === "*" && month === "*" && dow === "*") {
    return { main: `Daily · ${pad2(hour)}:${pad2(min)}`, sub };
  }
  if (isFixedMinute && isFixedHour && dom === "*" && month === "*" && isFixedDow) {
    const label = WEEKDAY_ABBR[Number(dow) % 7] ?? dow;
    return { main: `Weekly · ${label} ${pad2(hour)}:${pad2(min)}`, sub };
  }
  if (isFixedMinute && isFixedHour && isFixedDom && month === "*" && dow === "*") {
    return { main: `Monthly · day ${dom} ${pad2(hour)}:${pad2(min)}`, sub };
  }
  return { main: "Cron", sub };
}

// ---------------------------------------------------------------------------
// describeDelivery — see `DeliveryJson`'s docstring in use-scheduled-jobs.ts
// for why this is a best-effort reading of an otherwise schema-less field.
// ---------------------------------------------------------------------------

export function describeDelivery(delivery: DeliveryJson | null | undefined): { label: string; sub: string | null } {
  if (delivery?.drive) return { label: "Drive", sub: delivery.drive.folder ?? null };
  if (delivery?.email) {
    const to = delivery.email.to ?? null;
    const count = delivery.email.count;
    const sub = to ? `${to}${count ? ` (${count} recipient${count === 1 ? "" : "s"})` : ""}` : null;
    return { label: "Email", sub };
  }
  if (delivery?.recon) return { label: "Reconciliation", sub: delivery.recon.label ?? "run page" };
  if (delivery?.in_app) return { label: "Reports", sub: delivery.in_app.report_title ?? "in-app versions" };
  return { label: "—", sub: null };
}

// ---------------------------------------------------------------------------
// Run status — `Schedule.last_run_status` (spec §B4) is one of the executor's
// own reason-enum values (`done|budget|stall|error|blocked`), plus its
// in-flight bookkeeping values (`running`, `skipped`, `paused`,
// `retry_pending`). Never fabricate a status when there isn't one yet —
// `runStatusLabel(null)` says "never run", not "done".
// ---------------------------------------------------------------------------

export function runStatusTone(status: string | null | undefined): PillTone {
  switch (status) {
    case "done":
      return "ok";
    case "budget":
    case "retry_pending":
    case "paused":
      return "warn";
    case "running":
    case "skipped":
      return "mute";
    case "error":
    case "stall":
    case "blocked":
      return "crit";
    default:
      return "mute";
  }
}

export function runStatusLabel(status: string | null | undefined): string {
  if (!status) return "never run";
  if (status === "retry_pending") return "retrying";
  return status;
}

// ---------------------------------------------------------------------------
// Time formatting — "Mon 8 Sep · 06:00" style, and a forward countdown for
// the Next run tile ("in 5 d 6 h").
// ---------------------------------------------------------------------------

const WHEN_DATE_FMT: Intl.DateTimeFormatOptions = { weekday: "short", day: "numeric", month: "short" };
const WHEN_TIME_FMT: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit", hour12: false };

export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.toLocaleDateString(undefined, WHEN_DATE_FMT)} · ${d.toLocaleTimeString(undefined, WHEN_TIME_FMT)}`;
}

export function formatCountdown(iso: string | null | undefined, now: Date = new Date()): string | null {
  if (!iso) return null;
  const target = new Date(iso).getTime();
  if (Number.isNaN(target)) return null;
  const diffMs = target - now.getTime();
  if (diffMs <= 0) return "due now";
  const mins = Math.round(diffMs / 60_000);
  const days = Math.floor(mins / (60 * 24));
  const hours = Math.floor((mins % (60 * 24)) / 60);
  if (days > 0) return `in ${days} d ${hours} h`;
  if (hours > 0) return `in ${hours} h`;
  return `in ${Math.max(mins, 1)} m`;
}
