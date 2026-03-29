"use client";

import { CheckCircle2, AlertTriangle, XCircle, DollarSign } from "lucide-react";
import type { ReconRun } from "@/lib/types";

interface ReconSummaryBarProps {
  run: ReconRun | null;
}

export function ReconSummaryBar({ run }: ReconSummaryBarProps) {
  if (!run) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft text-center text-muted-foreground">
        No reconciliation run selected. Start a new run or select a previous one.
      </div>
    );
  }

  const cards = [
    {
      label: "Matched",
      value: run.matched_count,
      icon: CheckCircle2,
      color: "text-green-600",
      bg: "bg-green-50",
    },
    {
      label: "Exceptions",
      value: run.exception_count,
      icon: AlertTriangle,
      color: "text-orange-600",
      bg: "bg-orange-50",
    },
    {
      label: "Unmatched",
      value: run.unmatched_count,
      icon: XCircle,
      color: "text-red-600",
      bg: "bg-red-50",
    },
    {
      label: "Total Variance",
      value: `$${Number(run.total_variance).toLocaleString("en-US", { minimumFractionDigits: 2 })}`,
      icon: DollarSign,
      color: "text-blue-600",
      bg: "bg-blue-50",
    },
  ];

  return (
    <div className="grid grid-cols-4 gap-4">
      {cards.map(({ label, value, icon: Icon, color, bg }) => (
        <div key={label} className="rounded-xl border bg-card p-5 shadow-soft">
          <div className="flex items-center gap-3">
            <div className={`rounded-lg ${bg} p-2`}>
              <Icon className={`h-5 w-5 ${color}`} />
            </div>
            <div>
              <p className="text-[13px] text-muted-foreground">{label}</p>
              <p className="text-2xl font-semibold text-foreground">{value}</p>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
