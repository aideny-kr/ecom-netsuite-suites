"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, MessageSquare } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ResolutionGroupItems } from "@/components/reconciliation/resolution-group-items";
import type { ReconResolutionGroup, ReconResolutionProposal } from "@/lib/types";

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

const ACTION_LABEL: Record<string, string> = {
  book_fee_line: "Fee line",
  create_and_apply_deposit: "Create deposit",
  apply_deposit: "Apply deposit",
  writeoff_je: "Write-off",
  carry_forward: "Carry forward",
  needs_human: "Needs human",
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

function money(amount: string | number, currency: string) {
  return Number(amount).toLocaleString("en-US", { style: "currency", currency });
}

/** One click-to-copy identifier — displays `${prefix}${value}` but copies the
 * raw `value`, e.g. "NS#12345" displays with the prefix, but finance pastes
 * "12345" straight into a NetSuite internal-id search. */
function CopyableId({ prefix = "", value }: { prefix?: string; value: string }) {
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

interface ResolutionGroupsTableProps {
  runId: string;
  groups: ReconResolutionGroup[];
  // cardKey (`${group_key}:${currency}`) of the expanded group, or null.
  expandedKey: string | null;
  onToggleExpand: (cardKey: string) => void;
  isApproving: boolean;
  disabled?: boolean;
  tickedAboveByGroup: Record<string, string[]>;
  onTickAbove: (cardKey: string, proposalId: string, ticked: boolean) => void;
  groupResetSignals: Record<string, number>;
  onApprove: (group: ReconResolutionGroup, notes: string, includedAboveIds: string[]) => void;
  onReject: (group: ReconResolutionGroup) => void;
  onInvestigate: (proposal: ReconResolutionProposal) => void;
}

export function ResolutionGroupsTable({
  runId,
  groups,
  expandedKey,
  onToggleExpand,
  isApproving,
  disabled,
  tickedAboveByGroup,
  onTickAbove,
  groupResetSignals,
  onApprove,
  onReject,
  onInvestigate,
}: ResolutionGroupsTableProps) {
  // Notes are per-cardKey (a group_key alone can span more than one
  // currency) and live here rather than per-row, since every group in this
  // worksheet is rendered by one component instance, not one per group.
  const [notesByGroup, setNotesByGroup] = useState<Record<string, string>>({});
  const prevSignals = useRef<Record<string, number>>({});
  useEffect(() => {
    const changedKeys = Object.keys(groupResetSignals).filter(
      (key) => groupResetSignals[key] !== prevSignals.current[key]
    );
    if (changedKeys.length) {
      setNotesByGroup((prev) => {
        const next = { ...prev };
        for (const key of changedKeys) delete next[key];
        return next;
      });
    }
    prevSignals.current = groupResetSignals;
  }, [groupResetSignals]);

  if (!groups.length) {
    return <p className="text-[13px] text-muted-foreground">No resolution groups for this run.</p>;
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-[15px] font-medium text-foreground">
          Resolution groups
          <span className="ml-2 text-[13px] font-normal text-muted-foreground">
            · {groups.length.toLocaleString()} groups
          </span>
        </h2>
      </div>
      <div className="overflow-x-auto rounded-xl border bg-card shadow-soft">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead style={{ width: "30%" }}>Group</TableHead>
              <TableHead>Action</TableHead>
              <TableHead>Vehicle</TableHead>
              <TableHead>CCY</TableHead>
              <TableHead className="text-right">Items</TableHead>
              <TableHead className="text-right">Approved</TableHead>
              <TableHead className="text-right">Above mat.</TableHead>
              <TableHead className="text-right">Total</TableHead>
              <TableHead className="sr-only">Row actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {groups.map((group) => {
              const cardKey = `${group.group_key}:${group.currency}`;
              const expanded = expandedKey === cardKey;
              const isNeedsHuman = group.action === "needs_human";
              const isCarryForward = group.action === "carry_forward";
              const includedAboveIds = tickedAboveByGroup[cardKey] ?? [];
              const oneClickCount =
                group.proposed_count - group.above_materiality_count + includedAboveIds.length;
              const blocked = disabled || isApproving || oneClickCount <= 0;
              const vehicleChip = VEHICLE_CHIP[group.booking_vehicle] ?? VEHICLE_CHIP.none;
              const notes = notesByGroup[cardKey] ?? "";

              const approveButton = (source: "row" | "detail") => (
                <button
                  type="button"
                  onClick={() =>
                    onApprove(group, source === "detail" ? notes : "", includedAboveIds)
                  }
                  disabled={blocked}
                  className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {isApproving
                    ? "Working…"
                    : isCarryForward
                      ? `Acknowledge ${oneClickCount}`
                      : `Approve ${oneClickCount}`}
                </button>
              );
              const rejectButton = (
                <button
                  type="button"
                  onClick={() => onReject(group)}
                  disabled={disabled || isApproving}
                  className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Reject
                </button>
              );

              return (
                <Fragment key={cardKey}>
                  <TableRow className={expanded ? "bg-muted/40" : undefined}>
                    <TableCell>
                      <button
                        type="button"
                        onClick={() => onToggleExpand(cardKey)}
                        className="flex items-start gap-2 text-left"
                      >
                        {expanded ? (
                          <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                        ) : (
                          <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                        )}
                        <span>
                          <span className="font-medium text-foreground">
                            {ROOT_CAUSE_LABEL[group.root_cause] ?? group.root_cause}
                          </span>
                          {group.above_materiality_count > 0 && (
                            <span className="block text-xs text-amber-700">
                              {group.above_materiality_count} above materiality
                            </span>
                          )}
                        </span>
                      </button>
                    </TableCell>
                    <TableCell>
                      <span className="rounded-full border bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                        {ACTION_LABEL[group.action] ?? group.action}
                      </span>
                    </TableCell>
                    <TableCell>
                      <span className={`rounded-full border px-2 py-0.5 text-xs ${vehicleChip.className}`}>
                        {vehicleChip.label}
                      </span>
                    </TableCell>
                    <TableCell>{group.currency}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {group.count.toLocaleString()}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {group.approved_count.toLocaleString()}
                    </TableCell>
                    <TableCell
                      className={`text-right tabular-nums ${group.above_materiality_count > 0 ? "text-amber-700" : ""}`}
                    >
                      {group.above_materiality_count.toLocaleString()}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {money(group.total_amount, group.currency)}
                    </TableCell>
                    <TableCell className="text-right">
                      {isNeedsHuman ? (
                        <span className="text-xs text-muted-foreground">Review individually</span>
                      ) : (
                        !expanded && (
                          <div className="flex justify-end gap-2">
                            {rejectButton}
                            {approveButton("row")}
                          </div>
                        )
                      )}
                    </TableCell>
                  </TableRow>
                  {expanded && (
                    <TableRow>
                      <TableCell colSpan={9} className="bg-muted/20 p-0">
                        <div className="space-y-3 p-4">
                          {!isNeedsHuman && (
                            <div className="flex items-center gap-3">
                              <input
                                type="text"
                                value={notes}
                                onChange={(e) =>
                                  setNotesByGroup((prev) => ({ ...prev, [cardKey]: e.target.value }))
                                }
                                disabled={blocked}
                                placeholder="Optional note for the audit trail (e.g. month-end close)"
                                className="flex-1 rounded-md border bg-background px-3 py-1.5 text-[13px] text-foreground placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50"
                              />
                              {rejectButton}
                              {approveButton("detail")}
                            </div>
                          )}
                          <ResolutionGroupItems
                            runId={runId}
                            groupKey={group.group_key}
                            tickedAboveIds={includedAboveIds}
                            onTickAbove={(id, ticked) => onTickAbove(cardKey, id, ticked)}
                            onInvestigate={onInvestigate}
                          />
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

interface NeedsHumanWorksheetProps {
  proposals: ReconResolutionProposal[] | undefined;
  isLoading: boolean;
  onInvestigate: (proposal: ReconResolutionProposal) => void;
}

export function NeedsHumanWorksheet({ proposals, isLoading, onInvestigate }: NeedsHumanWorksheetProps) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-[15px] font-medium text-foreground">
          Needs human review
          {!!proposals?.length && (
            <span className="ml-2 text-[13px] font-normal text-muted-foreground">
              · {proposals.length.toLocaleString()} items
            </span>
          )}
        </h2>
      </div>
      {isLoading ? (
        <p className="text-[13px] text-muted-foreground">Loading…</p>
      ) : !proposals?.length ? (
        <p className="text-[13px] text-muted-foreground">No items need human review.</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-card shadow-soft">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Order ref</TableHead>
                <TableHead>Stripe charge</TableHead>
                <TableHead>NetSuite ID</TableHead>
                <TableHead className="text-right">Amount</TableHead>
                <TableHead>Root cause</TableHead>
                <TableHead>Why held</TableHead>
                <TableHead className="sr-only">Row actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {proposals.map((p) => (
                <TableRow key={p.id}>
                  <TableCell>
                    {p.order_reference ? (
                      <CopyableId value={p.order_reference} />
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {p.stripe_charge_id ? (
                      <CopyableId value={p.stripe_charge_id} />
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {p.netsuite_internal_id ? (
                      <span className="inline-flex items-center gap-1.5">
                        <CopyableId prefix="NS#" value={p.netsuite_internal_id} />
                        {p.netsuite_record_type && (
                          <span className="text-xs text-muted-foreground">{p.netsuite_record_type}</span>
                        )}
                      </span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {money(p.proposed_amount, p.currency || "USD")}
                  </TableCell>
                  <TableCell>
                    <span className="rounded-full border bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                      {ROOT_CAUSE_LABEL[p.root_cause] ?? p.root_cause}
                    </span>
                  </TableCell>
                  <TableCell className="max-w-xs truncate text-muted-foreground" title={p.narrative}>
                    {p.narrative}
                  </TableCell>
                  <TableCell className="text-right">
                    <button
                      type="button"
                      onClick={() => onInvestigate(p)}
                      className="inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
                    >
                      <MessageSquare className="h-3.5 w-3.5" />
                      Investigate in chat
                    </button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
