"use client";

import { useEffect, useState } from "react";
import { CheckCircle2 } from "lucide-react";

interface BulkApprovalCardProps {
  bucketLabel: string;
  count: number;
  totalVariance: number;
  currency?: string;
  // Notes are audit-only: passed up to the approve-bucket mutation so the
  // operator can annotate WHY a bucket was bulk-approved. Empty string when blank.
  onApprove: (notes: string) => void;
  isApproving: boolean;
  disabled?: boolean;
  // Audit-integrity guard: a note typed for one bucket/approval must NOT carry
  // into the next. The parent bumps this counter on every successful approve so
  // the card clears its local notes. (The parent also keys the card by active
  // bucket so a bucket switch remounts with fresh notes.)
  resetSignal?: number;
}

export function BulkApprovalCard({
  bucketLabel,
  count,
  totalVariance,
  currency = "USD",
  onApprove,
  isApproving,
  disabled,
  resetSignal,
}: BulkApprovalCardProps) {
  const [notes, setNotes] = useState("");
  // Clear the note after a successful approve so it can't ride into a re-approval
  // and mislabel the next immutable audit record.
  useEffect(() => {
    setNotes("");
  }, [resetSignal]);
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
          onClick={() => onApprove(notes)}
          disabled={blocked}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <CheckCircle2 className="h-4 w-4" />
          {isApproving ? "Approving…" : `Approve all ${count}`}
        </button>
      </div>
      <input
        type="text"
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        disabled={blocked}
        placeholder="Optional note for the audit trail (e.g. month-end close)"
        className="mt-3 w-full rounded-md border bg-background px-3 py-1.5 text-[13px] text-foreground placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50"
      />
    </div>
  );
}
