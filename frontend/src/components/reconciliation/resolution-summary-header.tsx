"use client";

import { CheckCircle2, Lightbulb, DollarSign, Loader2 } from "lucide-react";
import type { ReconResolutionSummary } from "@/lib/types";

interface ResolutionSummaryHeaderProps {
  summary: ReconResolutionSummary | null;
}

const money = (v: string | number, currency = "USD") =>
  Number(v).toLocaleString("en-US", { style: "currency", currency });

// variance_by_root_cause keys become "root_cause (CUR)" once a run spans more
// than one currency (see ResolutionSummaryResponse docstring) — recover the
// currency for correct chip formatting.
const CAUSE_CURRENCY_SUFFIX = /\s\(([A-Z]{3})\)$/;

export function ResolutionSummaryHeader({ summary }: ResolutionSummaryHeaderProps) {
  if (!summary) {
    return (
      <div className="rounded-xl border bg-card p-5 shadow-soft text-center text-muted-foreground">
        No reconciliation run selected. Start a new run or select a previous one.
      </div>
    );
  }
  const totalVariance = Object.values(summary.variance_by_root_cause).reduce(
    (acc, v) => acc + Number(v),
    0
  );
  // Gross total is only ever safe to render as one number when every group
  // shares a currency — otherwise show a per-currency breakdown instead of
  // silently summing USD + EUR under one symbol.
  const totalsByCurrency = summary.groups.reduce<Record<string, number>>((acc, g) => {
    acc[g.currency] = (acc[g.currency] ?? 0) + Number(g.total_amount);
    return acc;
  }, {});
  const distinctCurrencies = Object.keys(totalsByCurrency);
  const grossExceptionValue =
    distinctCurrencies.length > 1
      ? distinctCurrencies.map((c) => money(totalsByCurrency[c], c)).join(" · ")
      : money(totalVariance, distinctCurrencies[0] ?? "USD");
  const defaultCauseCurrency = distinctCurrencies.length === 1 ? distinctCurrencies[0] : "USD";
  const cards = [
    {
      label: "Match rate",
      value: `${summary.match_rate}%`,
      sub: `${summary.matches_count.toLocaleString()} of ${summary.total_results.toLocaleString()} lines`,
      icon: CheckCircle2,
      color: "text-green-600",
      bg: "bg-green-50",
    },
    {
      label: "Explained rate",
      value: `${summary.explained_rate}%`,
      sub: `${summary.explained_count.toLocaleString()} of ${summary.proposals_count.toLocaleString()} exceptions have a proposed resolution`,
      icon: Lightbulb,
      color: "text-indigo-600",
      bg: "bg-indigo-50",
    },
    {
      label: "Gross exception amount",
      value: grossExceptionValue,
      sub: "sum of proposed resolution amounts",
      icon: DollarSign,
      color: "text-blue-600",
      bg: "bg-blue-50",
    },
  ];
  const rootCauses = Object.entries(summary.variance_by_root_cause).sort(
    (a, b) => Number(b[1]) - Number(a[1])
  );
  return (
    <div className="space-y-3">
      {summary.agent_job?.status === "running" && (
        <div className="inline-flex items-center gap-1.5 rounded-full border bg-card px-3 py-1 text-[13px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          <span>
            Agent investigating… {summary.agent_job.processed}/{summary.agent_job.total}
          </span>
        </div>
      )}
      <div className="grid grid-cols-3 gap-4">
        {cards.map(({ label, value, sub, icon: Icon, color, bg }) => (
          <div key={label} className="rounded-xl border bg-card p-5 shadow-soft">
            <div className="flex items-center gap-3">
              <div className={`rounded-lg ${bg} p-2`}>
                <Icon className={`h-5 w-5 ${color}`} />
              </div>
              <div>
                <p className="text-[13px] text-muted-foreground">{label}</p>
                <p className="text-2xl font-semibold text-foreground">{value}</p>
                <p className="text-xs text-muted-foreground">{sub}</p>
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        {rootCauses.map(([cause, amount]) => {
          const parsed = cause.match(CAUSE_CURRENCY_SUFFIX);
          const causeCurrency = parsed ? parsed[1] : defaultCauseCurrency;
          return (
            <span
              key={cause}
              className="inline-flex items-center gap-1.5 rounded-full border bg-card px-3 py-1 text-[13px]"
            >
              <span className="font-medium">{cause}</span>
              <span className="text-muted-foreground">{money(amount, causeCurrency)}</span>
            </span>
          );
        })}
      </div>
    </div>
  );
}
