"use client";

import { CheckCircle2, Wand2, Sparkles, AlertTriangle, DollarSign } from "lucide-react";
import type { ReconBucketSummary, ReconRun } from "@/lib/types";

interface ReconSummaryBarProps {
  summary: ReconBucketSummary | null;
  run: ReconRun | null;
}

export function ReconSummaryBar({ summary, run }: ReconSummaryBarProps) {
  if (!summary) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft text-center text-muted-foreground">
        No reconciliation run selected. Start a new run or select a previous one.
      </div>
    );
  }

  // run.total_variance is the SIGNED-NET total for the whole run (matches the
  // evidence pack): MATCHED lines store abs() deltas but UNMATCHED payout lines
  // store the RAW signed amount, which can be negative on refund-heavy periods.
  // The per-bucket totals are GROSS (sum-of-absolutes) and must NOT be summed
  // here — that would disagree with the signed-net run total.
  const totalVariance = Number(run?.total_variance ?? 0);

  const cards = [
    {
      label: "Matches",
      value: summary.matches?.count ?? 0,
      icon: CheckCircle2,
      color: "text-green-600",
      bg: "bg-green-50",
    },
    {
      label: "Rules",
      value: summary.rules?.count ?? 0,
      icon: Wand2,
      color: "text-indigo-600",
      bg: "bg-indigo-50",
    },
    {
      label: "Auto-Classifications",
      value: summary.auto_classifications?.count ?? 0,
      icon: Sparkles,
      color: "text-amber-600",
      bg: "bg-amber-50",
    },
    {
      label: "Needs Review",
      value: summary.needs_review?.count ?? 0,
      icon: AlertTriangle,
      color: "text-red-600",
      bg: "bg-red-50",
    },
    {
      label: "Total Variance",
      // Currency-formatted signed-net total. The formatter preserves the sign in
      // front of the symbol (-$42.50) on refund-heavy / net-negative periods.
      value: totalVariance.toLocaleString("en-US", {
        style: "currency",
        currency: "USD",
      }),
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
