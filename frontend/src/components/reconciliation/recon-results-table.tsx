"use client";

import { MessageSquare, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ReconResult } from "@/lib/types";
import { useApproveResult } from "@/hooks/use-reconciliation";

interface ReconResultsTableProps {
  results: ReconResult[];
  onInvestigate?: (result: ReconResult) => void;
}

const statusColors: Record<string, string> = {
  auto_matched: "bg-green-100 text-green-800",
  suggested: "bg-orange-100 text-orange-800",
  pending: "bg-red-100 text-red-800",
  approved: "bg-blue-100 text-blue-800",
  locked: "bg-gray-100 text-gray-800",
};

export function ReconResultsTable({ results, onInvestigate }: ReconResultsTableProps) {
  const approveResult = useApproveResult();

  if (results.length === 0) {
    return (
      <div className="rounded-xl border bg-card p-8 text-center text-muted-foreground shadow-soft">
        No results to display.
      </div>
    );
  }

  return (
    <div className="rounded-xl border bg-card shadow-soft overflow-hidden">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b bg-muted/50">
            <th className="px-4 py-3 text-left font-medium text-muted-foreground">Status</th>
            <th className="px-4 py-3 text-left font-medium text-muted-foreground">Order</th>
            <th className="px-4 py-3 text-left font-medium text-muted-foreground">Match</th>
            <th className="px-4 py-3 text-right font-medium text-muted-foreground">Stripe</th>
            <th className="px-4 py-3 text-right font-medium text-muted-foreground">NetSuite</th>
            <th className="px-4 py-3 text-right font-medium text-muted-foreground">Variance</th>
            <th className="px-4 py-3 text-center font-medium text-muted-foreground">Confidence</th>
            <th className="px-4 py-3 text-center font-medium text-muted-foreground">Actions</th>
          </tr>
        </thead>
        <tbody>
          {results.map((result) => (
            <tr key={result.id} className="border-b last:border-0 hover:bg-muted/30 transition-colors">
              <td className="px-4 py-3">
                <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", statusColors[result.status] || "bg-gray-100")}>
                  {result.status}
                </span>
              </td>
              <td className="px-4 py-3">
                {result.evidence?.order_reference ? (
                  <span className="font-mono text-[11px] text-foreground">{result.evidence.order_reference}</span>
                ) : result.evidence?.charge_source_id ? (
                  <span className="font-mono text-[11px] text-muted-foreground">{result.evidence.charge_source_id.slice(0, 14)}...</span>
                ) : (
                  <span className="text-muted-foreground">-</span>
                )}
              </td>
              <td className="px-4 py-3 text-foreground">{result.match_type}</td>
              <td className="px-4 py-3 text-right font-mono text-foreground">
                {result.stripe_amount != null ? `$${Number(result.stripe_amount).toFixed(2)}` : "-"}
              </td>
              <td className="px-4 py-3 text-right font-mono text-foreground">
                {result.netsuite_amount != null ? `$${Number(result.netsuite_amount).toFixed(2)}` : "-"}
              </td>
              <td className="px-4 py-3 text-right font-mono text-foreground">
                {Number(result.variance_amount) > 0 ? (
                  <span className="text-red-600">${Number(result.variance_amount).toFixed(2)}</span>
                ) : (
                  "$0.00"
                )}
              </td>
              <td className="px-4 py-3 text-center">
                <span className={cn(
                  "font-mono text-xs",
                  Number(result.confidence) >= 0.95 ? "text-green-600" :
                  Number(result.confidence) >= 0.75 ? "text-orange-600" : "text-red-600"
                )}>
                  {(Number(result.confidence) * 100).toFixed(0)}%
                </span>
              </td>
              <td className="px-4 py-3 text-center">
                <div className="flex items-center justify-center gap-1">
                  {result.status === "suggested" && (
                    <button
                      onClick={() => approveResult.mutate({ result_id: result.id })}
                      className="rounded p-1 text-green-600 hover:bg-green-50 transition-colors"
                      title="Approve match"
                    >
                      <Check className="h-4 w-4" />
                    </button>
                  )}
                  {(result.status === "pending" || result.status === "suggested") && onInvestigate && (
                    <button
                      onClick={() => onInvestigate(result)}
                      className="rounded p-1 text-blue-600 hover:bg-blue-50 transition-colors"
                      title="Investigate in Chat"
                    >
                      <MessageSquare className="h-4 w-4" />
                    </button>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
