"use client";

/**
 * Scheduled Jobs platform (Slice 2, spec §B6, mock state two — "Delivery").
 * Pure display of `schedule.delivery_json` (`DeliveryJson`,
 * `hooks/use-scheduled-jobs.ts`) — the same schema-less field the list
 * page's "Delivers to" column already reads via `describeDelivery`, here
 * rendered one row per known key instead of the list's single summary
 * cell. The mock's Files/In app/Notify rows describe data
 * (rendered-artifact kinds, auto-refresh recipient policy) this API never
 * carries on `delivery_json` — rather than fabricate them, this panel
 * renders only what the field actually has, honestly, the same
 * simplification precedent `jobs-list.tsx`'s file docstring documents for
 * the list page's own two illustrative-vs-real gaps.
 */

import type { JSX } from "react";
import type { ScheduleDetail } from "@/hooks/use-scheduled-jobs";

export function DeliveryPanel({ schedule }: { schedule: ScheduleDetail }): JSX.Element {
  const delivery = schedule.delivery_json;
  const rows: Array<{ label: string; value: JSX.Element | string }> = [];

  if (delivery?.drive) {
    rows.push({
      label: "Drive",
      value: <span className="font-mono text-[12px]">{delivery.drive.folder ?? "—"}</span>,
    });
  }
  if (delivery?.email) {
    const count = delivery.email.count;
    rows.push({
      label: "Email",
      value: `${delivery.email.to ?? "—"}${count ? ` (${count} recipient${count === 1 ? "" : "s"})` : ""}`,
    });
  }
  if (delivery?.recon) {
    rows.push({ label: "Reconciliation", value: delivery.recon.label ?? "run page" });
  }
  if (delivery?.in_app) {
    rows.push({ label: "In app", value: delivery.in_app.report_title ?? "in-app versions" });
  }

  return (
    <div className="rounded-lg border bg-card">
      <h3 className="border-b px-3 py-2 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        Delivery
      </h3>
      <div className="p-3">
        {rows.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No delivery configured.</p>
        ) : (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3.5 gap-y-1.5 text-[12.5px]">
            {rows.map((row) => (
              <div key={row.label} className="contents">
                <dt className="pt-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-muted-foreground">
                  {row.label}
                </dt>
                <dd>{row.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    </div>
  );
}
