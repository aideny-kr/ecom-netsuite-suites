"use client";

import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import { useFreshnessStatus } from "@/hooks/use-operational-status";
import { ApiError } from "@/lib/api-client";

export function FreshnessAlertBanner() {
  const query = useFreshnessStatus();
  const denied = query.error instanceof ApiError && [401, 403].includes(query.error.status);
  const data = denied ? undefined : query.data;
  const alerts = data?.entities.filter(entity => entity.freshness?.state === "alert") || [];
  if (!alerts.length && !data?.truncated && !query.isError) return null;
  if (denied) return null;

  return (
    <aside aria-label="Reconciliation freshness" className="border-b border-amber-300/60 bg-amber-50 px-4 py-3 text-amber-950 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-100">
      <div className="mx-auto flex max-w-screen-xl items-start gap-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <div className="min-w-0 flex-1 space-y-1 text-[13px]">
          <p className="font-semibold">{alerts.length ? `Daily reconciliation needs attention · ${alerts.length} ${alerts.length === 1 ? "entity" : "entities"}` : "Reconciliation freshness needs checking"}</p>
          {query.isError && <p role="status">Freshness check failed.{data ? " These dates are from the last saved snapshot." : " Current coverage is unavailable."}</p>}
          <ul className="space-y-1">
            {alerts.slice(0, 3).map(entity => (
              <li key={entity.config_id}>
                <span className="font-medium">{entity.name}</span>: {entity.freshness?.reason === "daily_scan_stopped" ? "daily scan stopped" : "daily coverage overdue"}.
                {" "}Verified through {entity.coverage.checked_through || "no completed daily scan"}; expected {entity.coverage.expected_checked_through || "date unavailable"}.
              </li>
            ))}
          </ul>
          {alerts.length > 3 && <p>{alerts.length - 3} more {alerts.length - 3 === 1 ? "entity needs" : "entities need"} attention.</p>}
          {data?.truncated && <p>This check covers the first 50 enabled daily schedules. More entities are available in Ops status.</p>}
        </div>
        <Link href="/settings/ops-status" className="shrink-0 text-[13px] font-medium underline underline-offset-4">Ops status</Link>
      </div>
    </aside>
  );
}
