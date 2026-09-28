"use client";

import Link from "next/link";
import { Activity, ArrowRight } from "lucide-react";
import { useTransactionAccess } from "@/hooks/use-transaction-ops";

export function OpsStatusLink() {
  const { allowed } = useTransactionAccess();
  if (!allowed) return null;
  return (
    <Link href="/transaction-operations/status" className="group flex items-center gap-4 rounded-xl border bg-card p-5 shadow-soft transition-colors hover:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      <Activity className="h-5 w-5 shrink-0 text-primary" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <h3 className="text-lg font-semibold">Ops status</h3>
        <p className="mt-1 text-[13px] text-muted-foreground">Reconciliation coverage, current work and next scheduled actions.</p>
      </div>
      <ArrowRight className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
    </Link>
  );
}
