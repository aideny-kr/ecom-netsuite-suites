"use client";

import { useState } from "react";
import { Check, MessageSquare } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useGroupProposals } from "@/hooks/use-resolution";
import { fxChip, money } from "@/components/reconciliation/format";
import { RejectMatchControl } from "@/components/reconciliation/reject-match-control";
import type { ReconResolutionProposal } from "@/lib/types";

// Statuses that reach this worksheet are "proposed" (awaiting a decision) or
// "approved" (already actioned) — superseded/rejected proposals are filtered
// out server-side before they reach the group-proposals list.
const STATUS_CHIP: Record<string, { label: string; className: string }> = {
  proposed: { label: "Proposed", className: "bg-muted text-muted-foreground border-transparent" },
  approved: {
    label: "Approved",
    className:
      "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-800",
  },
};

function statusChip(status: string) {
  return STATUS_CHIP[status] ?? { label: status, className: "bg-muted text-muted-foreground border-transparent" };
}

function materialityChip(above: boolean) {
  return above
    ? {
        label: "Above materiality",
        className:
          "bg-amber-50 text-amber-700 border-amber-300 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800",
      }
    : { label: "Within materiality", className: "bg-muted text-muted-foreground border-transparent" };
}

/** One click-to-copy identifier cell. Displays `${prefix}${value}` but
 * copies the raw `value` — e.g. "NS#12345" displays with the prefix, but
 * finance pastes "12345" straight into a NetSuite internal-id search. */
function IdentifierSegment({ prefix = "", value }: { prefix?: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Permissions/insecure-context failures land here — leave `copied`
      // false rather than show a checkmark for a copy that didn't happen.
    }
  };
  return (
    <button
      type="button"
      onClick={handleCopy}
      title={`Copy ${value}`}
      className="inline-flex items-center gap-0.5 font-mono text-xs hover:text-foreground"
    >
      {prefix}
      {value}
      {copied && <Check className="h-3 w-3 text-green-500" />}
    </button>
  );
}

interface ResolutionGroupItemsProps {
  runId: string;
  groupKey: string;
  // Scopes the panel's item fetch to the expanded group's own currency — on
  // a multi-currency run, group_key alone can span more than one currency
  // (the per-group export already narrows the same way).
  currency: string;
  tickedAboveIds: string[];
  onTickAbove: (proposalId: string, ticked: boolean) => void;
  onInvestigate: (proposal: ReconResolutionProposal) => void;
  // Closed run / recon disabled — same freeze the group's Approve honours. The
  // reject endpoint refuses a closed period server-side, so the control is not
  // rendered rather than offered and then refused.
  disabled?: boolean;
}

export function ResolutionGroupItems({
  runId,
  groupKey,
  currency,
  tickedAboveIds,
  onTickAbove,
  onInvestigate,
  disabled,
}: ResolutionGroupItemsProps) {
  const { data: proposals, isLoading } = useGroupProposals(runId, groupKey, currency);
  if (isLoading) {
    return <p className="text-[13px] text-muted-foreground">Loading items…</p>;
  }
  if (!proposals?.length) {
    return <p className="text-[13px] text-muted-foreground">No items in this group.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="sr-only">Select</TableHead>
            <TableHead>Order ref</TableHead>
            <TableHead>Stripe charge</TableHead>
            <TableHead>NetSuite ID</TableHead>
            <TableHead className="text-right">Stripe amt</TableHead>
            <TableHead className="text-right">NetSuite amt</TableHead>
            <TableHead className="text-right">Variance</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Materiality</TableHead>
            <TableHead>Narrative</TableHead>
            <TableHead className="sr-only">Row actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {proposals.map((p) => {
            const status = statusChip(p.status);
            const materiality = materialityChip(p.above_materiality);
            const fx = fxChip(p);
            return (
              <TableRow key={p.id}>
                <TableCell>
                  {p.above_materiality && p.status === "proposed" && (
                    <input
                      type="checkbox"
                      checked={tickedAboveIds.includes(p.id)}
                      onChange={(e) => onTickAbove(p.id, e.target.checked)}
                      aria-label={`Include ${p.order_reference ?? p.id} in approval`}
                    />
                  )}
                </TableCell>
                <TableCell>
                  {p.order_reference ? (
                    <IdentifierSegment value={p.order_reference} />
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell>
                  {p.stripe_charge_id ? (
                    <IdentifierSegment value={p.stripe_charge_id} />
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell>
                  {p.netsuite_internal_id ? (
                    <span className="inline-flex max-w-full items-center gap-1.5">
                      <IdentifierSegment prefix="NS#" value={p.netsuite_internal_id} />
                      {p.netsuite_record_type && (
                        <span className="shrink-0 text-xs text-muted-foreground">{p.netsuite_record_type}</span>
                      )}
                      {fx && (
                        <span
                          className="inline-block max-w-[7rem] shrink-0 truncate rounded-full bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground"
                          title={fx.title}
                        >
                          {fx.label}
                        </span>
                      )}
                    </span>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {money(p.stripe_amount, p.currency)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {money(p.netsuite_amount, p.currency)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {money(p.variance_amount, p.currency)}
                </TableCell>
                <TableCell>
                  <span
                    className={`inline-block max-w-full truncate rounded-full border px-2 py-0.5 text-xs ${status.className}`}
                    title={status.label}
                  >
                    {status.label}
                  </span>
                </TableCell>
                <TableCell>
                  <span
                    className={`inline-block max-w-full truncate rounded-full border px-2 py-0.5 text-xs ${materiality.className}`}
                    title={materiality.label}
                  >
                    {materiality.label}
                  </span>
                </TableCell>
                <TableCell className="max-w-xs truncate text-muted-foreground" title={p.narrative}>
                  {p.narrative}
                </TableCell>
                <TableCell className="text-right">
                  {/* min-w-max: this table is auto-layout, so the column sizes to its
                      content's MIN-content width — and a flex row whose buttons may
                      wrap their text reports a min-content far narrower than the
                      buttons actually render at. The column then came out too narrow
                      and the shrink-0 buttons overflowed left onto the Narrative
                      column. min-w-max makes the column claim the width it will use. */}
                  <div className="flex min-w-max items-center justify-end gap-1.5">
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
                    {/* Targets the RESULT, not this proposal — the two ids sit side by
                        side on `p` and only result_id is what PATCH /results/{id}/reject
                        keys on. Gated on the proposal still being undecided: group
                        approve flips the underlying result to 'approved', which is
                        terminal, and the proposal's own status is the only signal this
                        surface carries about that. */}
                    {p.status === "proposed" && (
                      <RejectMatchControl resultId={p.result_id} disabled={disabled} variant="inline" />
                    )}
                  </div>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
