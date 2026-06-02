"use client";

import { CheckCircle2 } from "lucide-react";

interface BulkApprovalCardProps {
  bucketLabel: string;
  count: number;
  totalVariance: number;
  currency?: string;
  onApprove: () => void;
  isApproving: boolean;
  disabled?: boolean;
}

export function BulkApprovalCard({
  bucketLabel,
  count,
  totalVariance,
  currency = "USD",
  onApprove,
  isApproving,
  disabled,
}: BulkApprovalCardProps) {
  const money = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
  }).format(totalVariance);
  const blocked = disabled || isApproving || count === 0;

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="text-[15px] font-medium text-foreground">{bucketLabel}</p>
          <p className="text-[13px] text-muted-foreground">
            {count} lines · total variance {money}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            Approving creates one audit record per line. No postings are made.
          </p>
        </div>
        <button
          type="button"
          onClick={onApprove}
          disabled={blocked}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <CheckCircle2 className="h-4 w-4" />
          {isApproving ? "Approving…" : `Approve all ${count}`}
        </button>
      </div>
    </div>
  );
}
