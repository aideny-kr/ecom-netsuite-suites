"use client";

import { useState } from "react";
import { Check, MessageSquare } from "lucide-react";
import { useGroupProposals } from "@/hooks/use-resolution";
import type { ReconResolutionProposal } from "@/lib/types";

/** One click-to-copy identifier segment. Displays `${prefix}${value}` but
 * copies the raw `value` — e.g. "NS#12345" displays with the prefix, but
 * finance pastes "12345" straight into a NetSuite internal-id search. */
function IdentifierSegment({ prefix = "", value }: { prefix?: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <button
      type="button"
      onClick={handleCopy}
      title={`Copy ${value}`}
      className="inline-flex items-center gap-0.5 hover:text-foreground"
    >
      {prefix}
      {value}
      {copied && <Check className="h-3 w-3 text-green-500" />}
    </button>
  );
}

function IdentifierLine({ proposal }: { proposal: ReconResolutionProposal }) {
  const segments: { key: string; prefix?: string; value: string }[] = [];
  if (proposal.order_reference) segments.push({ key: "order", value: proposal.order_reference });
  if (proposal.stripe_charge_id) segments.push({ key: "charge", value: proposal.stripe_charge_id });
  if (proposal.netsuite_internal_id) {
    segments.push({ key: "netsuite", prefix: "NS#", value: proposal.netsuite_internal_id });
  }
  if (!segments.length) return null;
  return (
    <p className="mt-0.5 flex items-center gap-1.5 font-mono text-[11px] text-muted-foreground">
      {segments.map((s, i) => (
        <span key={s.key} className="inline-flex items-center gap-1.5">
          {i > 0 && <span aria-hidden>·</span>}
          <IdentifierSegment prefix={s.prefix} value={s.value} />
        </span>
      ))}
    </p>
  );
}

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
              <IdentifierLine proposal={p} />
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
