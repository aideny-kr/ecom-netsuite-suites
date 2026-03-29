"use client";

import { AlertTriangle, MessageSquare } from "lucide-react";
import type { ReconResult } from "@/lib/types";

interface ReconExceptionCardProps {
  result: ReconResult;
  onInvestigate?: (result: ReconResult) => void;
}

export function ReconExceptionCard({ result, onInvestigate }: ReconExceptionCardProps) {
  return (
    <div className="rounded-xl border border-orange-200 bg-orange-50/50 p-5 shadow-soft">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <AlertTriangle className="h-5 w-5 text-orange-600" />
          <h3 className="text-[15px] font-semibold text-foreground">
            {result.variance_type || "Exception"}
          </h3>
        </div>
        <span className="rounded-full bg-orange-100 px-2 py-0.5 text-xs font-medium text-orange-800">
          {(Number(result.confidence) * 100).toFixed(0)}% confidence
        </span>
      </div>

      <div className="mt-4 grid grid-cols-3 gap-4 text-[13px]">
        <div>
          <p className="text-muted-foreground">Stripe Amount</p>
          <p className="font-mono font-medium text-foreground">
            {result.stripe_amount != null ? `$${Number(result.stripe_amount).toFixed(2)}` : "N/A"}
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">NetSuite Amount</p>
          <p className="font-mono font-medium text-foreground">
            {result.netsuite_amount != null ? `$${Number(result.netsuite_amount).toFixed(2)}` : "N/A"}
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">Variance</p>
          <p className="font-mono font-medium text-red-600">
            ${Number(result.variance_amount).toFixed(2)}
          </p>
        </div>
      </div>

      {result.variance_explanation && (
        <p className="mt-3 text-[13px] text-muted-foreground">
          {result.variance_explanation}
        </p>
      )}

      <div className="mt-4 flex gap-2">
        {onInvestigate && (
          <button
            onClick={() => onInvestigate(result)}
            className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 transition-colors"
          >
            <MessageSquare className="h-3.5 w-3.5" />
            Investigate in Chat
          </button>
        )}
      </div>
    </div>
  );
}
