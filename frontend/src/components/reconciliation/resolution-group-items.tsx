"use client";

import { MessageSquare } from "lucide-react";
import { useGroupProposals } from "@/hooks/use-resolution";
import type { ReconResolutionProposal } from "@/lib/types";

interface ResolutionGroupItemsProps {
  runId: string;
  groupKey: string;
  tickedAboveIds: string[];
  onTickAbove: (proposalId: string, ticked: boolean) => void;
  onInvestigate: (proposal: ReconResolutionProposal) => void;
}

export function ResolutionGroupItems({
  runId,
  groupKey,
  tickedAboveIds,
  onTickAbove,
  onInvestigate,
}: ResolutionGroupItemsProps) {
  const { data: proposals, isLoading } = useGroupProposals(runId, groupKey);
  if (isLoading) {
    return <p className="text-[13px] text-muted-foreground">Loading items…</p>;
  }
  if (!proposals?.length) {
    return <p className="text-[13px] text-muted-foreground">No items in this group.</p>;
  }
  const money = (p: ReconResolutionProposal) =>
    Number(p.proposed_amount).toLocaleString("en-US", {
      style: "currency",
      currency: p.currency || "USD",
    });
  return (
    <ul className="divide-y">
      {proposals.map((p) => (
        <li key={p.id} className="flex items-center justify-between gap-3 py-2">
          <div className="flex items-center gap-3">
            {p.above_materiality && p.status === "proposed" && (
              <input
                type="checkbox"
                checked={tickedAboveIds.includes(p.id)}
                onChange={(e) => onTickAbove(p.id, e.target.checked)}
                aria-label={`Include ${money(p)} item in approval`}
              />
            )}
            <div>
              <p className="text-[13px] text-foreground">{p.narrative}</p>
              <p className="text-xs text-muted-foreground">
                {money(p)}
                {p.above_materiality && (
                  <span className="ml-2 text-amber-700">above materiality</span>
                )}
                {p.status !== "proposed" && <span className="ml-2">· {p.status}</span>}
              </p>
            </div>
          </div>
          {p.action === "needs_human" && (
            <button
              type="button"
              onClick={() => onInvestigate(p)}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
            >
              <MessageSquare className="h-3.5 w-3.5" />
              Investigate in chat
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
