"use client";

import { AlertTriangle, MessageSquare } from "lucide-react";
import type { ReconResult } from "@/lib/types";

interface ReconExceptionCardProps {
  result: ReconResult;
  onInvestigate?: (result: ReconResult) => void;
}

export function ReconExceptionCard({ result, onInvestigate }: ReconExceptionCardProps) {
  const orderRef = result.evidence?.order_reference;
  const chargeId = result.evidence?.charge_source_id || result.evidence?.payout_source_id;
  const isUnmatched = result.match_type === "unmatched";
  const label = isUnmatched ? "Unmatched" : (result.variance_type || "Exception");

  return (
    <div className="rounded-xl border bg-card p-5 shadow-soft">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 text-orange-500" />
          <h3 className="text-[15px] font-semibold text-foreground capitalize">{label}</h3>
          {orderRef && (
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              {orderRef}
            </span>
          )}
        </div>
        {!isUnmatched && (
          <span
            className="rounded-full bg-orange-500/10 px-2 py-0.5 text-[11px] font-medium text-orange-600"
            title="Advisory match score (amount + timing agreement)."
          >
            {(Number(result.confidence) * 100).toFixed(0)}% confidence
          </span>
        )}
      </div>

      {chargeId && (
        <p className="mt-1.5 font-mono text-[11px] text-muted-foreground">{chargeId}</p>
      )}

      <div className="mt-3 grid grid-cols-3 gap-4 text-[13px]">
        <div>
          <p className="text-muted-foreground">Stripe</p>
          <p className="font-mono font-medium text-foreground">
            {result.stripe_amount != null
              ? `$${Number(result.stripe_amount).toLocaleString("en-US", { minimumFractionDigits: 2 })}`
              : "N/A"}
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">NetSuite</p>
          <p className="font-mono font-medium text-foreground">
            {result.netsuite_amount != null
              ? `$${Number(result.netsuite_amount).toLocaleString("en-US", { minimumFractionDigits: 2 })}`
              : "N/A"}
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">Variance</p>
          <p className={`font-mono font-medium ${Number(result.variance_amount) > 0 ? "text-red-500" : "text-emerald-500"}`}>
            ${Number(result.variance_amount).toLocaleString("en-US", { minimumFractionDigits: 2 })}
          </p>
        </div>
      </div>

      {result.variance_explanation && (
        <p className="mt-2 text-[12px] text-muted-foreground">{result.variance_explanation}</p>
      )}

      <div className="mt-3">
        {onInvestigate && (
          <button
            onClick={() => onInvestigate(result)}
            className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-blue-700 transition-colors"
          >
            <MessageSquare className="h-3 w-3" />
            Investigate in Chat
          </button>
        )}
      </div>
    </div>
  );
}
