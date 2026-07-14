"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, ChevronDown, ChevronRight, XCircle } from "lucide-react";
import type { ReconResolutionGroup } from "@/lib/types";

// Booking-vehicle chip copy + styling. journalentry is the flagged fallback —
// finance must SEE what is being booked as a raw JE (spec: amber chip).
const VEHICLE_CHIP: Record<string, { label: string; className: string }> = {
  deposit: { label: "Deposit fee line", className: "bg-blue-50 text-blue-700 border-blue-200" },
  customerdeposit: { label: "Customer deposit", className: "bg-blue-50 text-blue-700 border-blue-200" },
  depositapplication: { label: "Deposit application", className: "bg-blue-50 text-blue-700 border-blue-200" },
  creditmemo: { label: "Credit memo + refund", className: "bg-blue-50 text-blue-700 border-blue-200" },
  journalentry: { label: "Journal entry (fallback)", className: "bg-amber-50 text-amber-700 border-amber-300" },
  none: { label: "No booking", className: "bg-muted text-muted-foreground border-transparent" },
};

const ROOT_CAUSE_LABEL: Record<string, string> = {
  fees: "Stripe processing fees",
  missing: "Missing NetSuite deposit",
  fx_rounding: "FX / rounding",
  timing: "Timing differences",
  duplicate: "Duplicate deposits",
  chargeback: "Chargebacks / disputes",
  manual_adjustment: "Unexplained",
  missing_in_netsuite: "Missing in NetSuite",
  amount_mismatch: "Amount mismatch",
};

interface ResolutionGroupCardProps {
  group: ReconResolutionGroup;
  onApprove: (notes: string, includedAboveIds: string[]) => void;
  onReject: () => void;
  isApproving: boolean;
  disabled?: boolean;
  expanded: boolean;
  onToggleExpand: () => void;
  // Ticked above-materiality proposal ids, owned by the drill-down (children).
  includedAboveIds?: string[];
  children?: React.ReactNode;
  resetSignal?: number;
}

export function ResolutionGroupCard({
  group,
  onApprove,
  onReject,
  isApproving,
  disabled,
  expanded,
  onToggleExpand,
  includedAboveIds = [],
  children,
  resetSignal,
}: ResolutionGroupCardProps) {
  const [notes, setNotes] = useState("");
  useEffect(() => {
    setNotes("");
  }, [resetSignal]);

  const money = Number(group.total_amount).toLocaleString("en-US", {
    style: "currency",
    currency: group.currency,
  });
  const chip = VEHICLE_CHIP[group.booking_vehicle] ?? VEHICLE_CHIP.none;
  const isNeedsHuman = group.action === "needs_human";
  const isCarryForward = group.action === "carry_forward";
  const oneClickCount =
    group.proposed_count - group.above_materiality_count + includedAboveIds.length;
  const blocked = disabled || isApproving || oneClickCount <= 0;

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-start justify-between gap-4">
        <button type="button" onClick={onToggleExpand} className="flex items-start gap-2 text-left">
          {expanded ? (
            <ChevronDown className="mt-1 h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="mt-1 h-4 w-4 text-muted-foreground" />
          )}
          <div>
            <div className="flex items-center gap-2">
              <p className="text-[15px] font-medium text-foreground">
                {ROOT_CAUSE_LABEL[group.root_cause] ?? group.root_cause}
              </p>
              <span className={`rounded-full border px-2 py-0.5 text-xs ${chip.className}`}>
                {chip.label}
              </span>
            </div>
            <p className="text-[13px] text-muted-foreground">
              {group.count.toLocaleString()} items · {money}
              {group.approved_count > 0 && ` · ${group.approved_count} already approved`}
            </p>
            {group.above_materiality_count > 0 && !isNeedsHuman && (
              <p className="mt-1 text-xs text-amber-700">
                {group.above_materiality_count} above materiality — tick them individually in the item list.
              </p>
            )}
          </div>
        </button>
        <div className="flex shrink-0 items-center gap-2">
          {isNeedsHuman ? (
            <span className="text-[13px] text-muted-foreground">Review individually</span>
          ) : (
            <>
              <button
                type="button"
                onClick={onReject}
                disabled={disabled || isApproving}
                className="inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
              >
                <XCircle className="h-4 w-4" />
                Reject
              </button>
              <button
                type="button"
                onClick={() => onApprove(notes, includedAboveIds)}
                disabled={blocked}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <CheckCircle2 className="h-4 w-4" />
                {isApproving
                  ? "Working…"
                  : isCarryForward
                    ? `Acknowledge ${oneClickCount}`
                    : `Approve ${oneClickCount}`}
              </button>
            </>
          )}
        </div>
      </div>
      {!isNeedsHuman && (
        <input
          type="text"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          disabled={blocked}
          placeholder="Optional note for the audit trail (e.g. month-end close)"
          className="mt-3 w-full rounded-md border bg-background px-3 py-1.5 text-[13px] text-foreground placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50"
        />
      )}
      {expanded && <div className="mt-4 border-t pt-4">{children}</div>}
    </div>
  );
}
