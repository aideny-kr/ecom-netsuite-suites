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
import { ExportMenu } from "@/components/reconciliation/export-menu";
import { NEEDS_HUMAN_PROPOSALS_LIMIT } from "@/hooks/use-resolution";
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

// Muted descriptor shown next to the bold root-cause label in the groups
// worksheet (design mock: "Fee variance — Stripe fee not booked" etc).
// Unmapped root causes render with no descriptor rather than a guess.
const ROOT_CAUSE_DESCRIPTOR: Record<string, string> = {
  fees: "Stripe fee not booked",
  missing_in_netsuite: "deposit not found",
  chargeback: "disputed charge",
};

// Recency-hold carry_forwards (rule-7 sync-lag) reuse the "missing" /
// "missing_in_netsuite" root causes but describe a payout still in transit,
// not an unresolved deposit search — mirrors resolution_planner.py's
// RECENCY_HOLD_ROOT_CAUSES, which gates the same distinction server-side.
const RECENCY_HOLD_ROOT_CAUSES = new Set(["missing", "missing_in_netsuite"]);

function groupDescriptor(rootCause: string, action: string): string | null {
  if (action === "carry_forward" && RECENCY_HOLD_ROOT_CAUSES.has(rootCause)) {
    return "payout not yet settled";
  }
  return ROOT_CAUSE_DESCRIPTOR[rootCause] ?? null;
}

// Severity coloring for root-cause chips in the needs-human worksheet.
// Anything not listed renders with the neutral fallback chip.
const ROOT_CAUSE_SEVERITY: Record<string, "crit" | "warn"> = {
  chargeback: "crit",
  amount_mismatch: "warn",
  missing: "warn",
  missing_in_netsuite: "warn",
};

const SEVERITY_CHIP_CLASS: Record<"crit" | "warn" | "neutral", string> = {
  crit: "bg-red-50 text-red-700 border-red-300",
  warn: "bg-amber-50 text-amber-700 border-amber-300",
  neutral: "bg-muted text-muted-foreground border-transparent",
};

function rootCauseChipClass(rootCause: string): string {
  return SEVERITY_CHIP_CLASS[ROOT_CAUSE_SEVERITY[rootCause] ?? "neutral"];
}

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
      className="inline-flex max-w-full items-center gap-0.5 truncate font-mono text-[11px] hover:text-foreground"
    >
      {prefix}
      {value}
      {copied && <Check className="h-3 w-3 shrink-0 text-green-500" />}
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
        <ExportMenu
          runId={runId}
          params={{ section: "groups" }}
          labels={{ csv: "CSV — all groups", xlsx: "Excel — all groups" }}
        />
      </div>
      {/* table-fixed + explicit per-column widths (summing to 100%) so the
          worksheet — including the expanded panel's notes/Reject/Export row,
          which shares this table's column grid via colSpan — never forces
          this container wider than its 1440px-viewport content area. Only
          the items sub-table below (its own overflow-x-auto) may scroll. */}
      <div className="overflow-x-auto rounded-xl border bg-card shadow-soft">
        <Table className="table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead className="w-[24%] px-3 py-2">Group</TableHead>
              <TableHead className="w-[10%] px-3 py-2">Action</TableHead>
              <TableHead className="w-[12%] px-3 py-2">Vehicle</TableHead>
              <TableHead className="w-[6%] px-3 py-2">CCY</TableHead>
              <TableHead className="w-[7%] px-3 py-2 text-right">Items</TableHead>
              <TableHead className="w-[9%] px-3 py-2 text-right">Approved</TableHead>
              <TableHead className="w-[9%] px-3 py-2 text-right">Above mat.</TableHead>
              <TableHead className="w-[11%] px-3 py-2 text-right">Total</TableHead>
              <TableHead className="w-[12%] px-3 py-2 sr-only">Row actions</TableHead>
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
              const descriptor = groupDescriptor(group.root_cause, group.action);

              // Single reachable Approve/Acknowledge affordance per group — it stays
              // fixed in the row's last column across both collapsed and expanded
              // states (no `!expanded &&` swap) and always consumes whatever notes
              // are currently typed in the expanded panel, so there is never a
              // second Approve control to create a double-submit path.
              const approveButton = (
                <button
                  type="button"
                  onClick={() => onApprove(group, notes, includedAboveIds)}
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
                    <TableCell className="px-3 py-2">
                      <button
                        type="button"
                        onClick={() => onToggleExpand(cardKey)}
                        className="flex w-full items-start gap-2 text-left"
                      >
                        {expanded ? (
                          <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                        ) : (
                          <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                        )}
                        <span className="min-w-0">
                          <span
                            className="block truncate"
                            title={`${ROOT_CAUSE_LABEL[group.root_cause] ?? group.root_cause}${descriptor ? ` — ${descriptor}` : ""}`}
                          >
                            <span className="font-medium text-foreground">
                              {ROOT_CAUSE_LABEL[group.root_cause] ?? group.root_cause}
                            </span>
                            {descriptor && (
                              <span className="text-muted-foreground"> — {descriptor}</span>
                            )}
                          </span>
                          {group.above_materiality_count > 0 && !isNeedsHuman && (
                            <span
                              className="block truncate text-xs text-amber-700"
                              title={`${group.above_materiality_count} above materiality — tick them individually in the item list.`}
                            >
                              {group.above_materiality_count} above materiality — tick them
                              individually in the item list.
                            </span>
                          )}
                        </span>
                      </button>
                    </TableCell>
                    <TableCell className="px-3 py-2">
                      <span className="rounded-full border bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                        {ACTION_LABEL[group.action] ?? group.action}
                      </span>
                    </TableCell>
                    <TableCell className="px-3 py-2">
                      <span className={`rounded-full border px-2 py-0.5 text-xs ${vehicleChip.className}`}>
                        {vehicleChip.label}
                      </span>
                    </TableCell>
                    <TableCell className="px-3 py-2">{group.currency}</TableCell>
                    <TableCell className="px-3 py-2 text-right tabular-nums">
                      {group.count.toLocaleString()}
                    </TableCell>
                    <TableCell className="px-3 py-2 text-right tabular-nums">
                      {group.approved_count.toLocaleString()}
                    </TableCell>
                    <TableCell
                      className={`px-3 py-2 text-right tabular-nums ${group.above_materiality_count > 0 ? "text-amber-700" : ""}`}
                    >
                      {group.above_materiality_count.toLocaleString()}
                    </TableCell>
                    <TableCell className="px-3 py-2 text-right tabular-nums">
                      {money(group.total_amount, group.currency)}
                    </TableCell>
                    <TableCell className="px-3 py-2 text-right">
                      {isNeedsHuman ? (
                        <span className="text-xs text-muted-foreground">Review individually</span>
                      ) : (
                        <div className="flex justify-end gap-2">{approveButton}</div>
                      )}
                    </TableCell>
                  </TableRow>
                  {expanded && (
                    <TableRow>
                      <TableCell colSpan={9} className="bg-muted/20 p-0">
                        <div className="space-y-3 p-3">
                          {!isNeedsHuman && (
                            <div className="flex items-center gap-2">
                              <input
                                type="text"
                                value={notes}
                                onChange={(e) =>
                                  setNotesByGroup((prev) => ({ ...prev, [cardKey]: e.target.value }))
                                }
                                disabled={blocked}
                                placeholder="Optional note for the audit trail (e.g. month-end close)"
                                className="min-w-0 flex-1 rounded-md border bg-background px-3 py-1.5 text-[13px] text-foreground placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50"
                              />
                              {rejectButton}
                              <ExportMenu
                                runId={runId}
                                params={{ section: "proposals", group_key: group.group_key, currency: group.currency }}
                              />
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
  runId: string;
  proposals: ReconResolutionProposal[] | undefined;
  isLoading: boolean;
  onInvestigate: (proposal: ReconResolutionProposal) => void;
}

export function NeedsHumanWorksheet({ runId, proposals, isLoading, onInvestigate }: NeedsHumanWorksheetProps) {
  // The fetch requests NEEDS_HUMAN_PROPOSALS_LIMIT rows; a returned count
  // that reaches the limit means the run may have more the operator can't
  // see in this worksheet — the export always carries the complete list.
  const atLimit = (proposals?.length ?? 0) >= NEEDS_HUMAN_PROPOSALS_LIMIT;
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
        <ExportMenu runId={runId} params={{ section: "proposals", action: "needs_human" }} />
      </div>
      {atLimit && (
        <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-[13px] text-amber-800">
          Showing first {NEEDS_HUMAN_PROPOSALS_LIMIT.toLocaleString()} — download the export for the complete list.
        </p>
      )}
      {isLoading ? (
        <p className="text-[13px] text-muted-foreground">Loading…</p>
      ) : !proposals?.length ? (
        <p className="text-[13px] text-muted-foreground">No items need human review.</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-card shadow-soft">
          <Table className="table-fixed">
            <TableHeader>
              <TableRow>
                <TableHead className="w-[11%] px-3 py-2">Order ref</TableHead>
                <TableHead className="w-[15%] px-3 py-2">Stripe charge</TableHead>
                <TableHead className="w-[13%] px-3 py-2">NetSuite ID</TableHead>
                <TableHead className="w-[9%] px-3 py-2 text-right">Amount</TableHead>
                <TableHead className="w-[12%] px-3 py-2">Root cause</TableHead>
                <TableHead className="w-[24%] px-3 py-2">Why held</TableHead>
                <TableHead className="w-[16%] px-3 py-2 sr-only">Row actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {proposals.map((p) => (
                <TableRow key={p.id}>
                  <TableCell className="px-3 py-2">
                    {p.order_reference ? (
                      <CopyableId value={p.order_reference} />
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="px-3 py-2">
                    {p.stripe_charge_id ? (
                      <CopyableId value={p.stripe_charge_id} />
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="px-3 py-2">
                    {p.netsuite_internal_id ? (
                      <span className="inline-flex max-w-full items-center gap-1.5">
                        <CopyableId prefix="NS#" value={p.netsuite_internal_id} />
                        {p.netsuite_record_type && (
                          <span className="shrink-0 text-xs text-muted-foreground">{p.netsuite_record_type}</span>
                        )}
                      </span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="px-3 py-2 text-right tabular-nums">
                    {money(p.proposed_amount, p.currency || "USD")}
                  </TableCell>
                  <TableCell className="px-3 py-2">
                    <span
                      className={`rounded-full border px-2 py-0.5 text-xs ${rootCauseChipClass(p.root_cause)}`}
                    >
                      {ROOT_CAUSE_LABEL[p.root_cause] ?? p.root_cause}
                    </span>
                  </TableCell>
                  <TableCell className="truncate px-3 py-2 text-muted-foreground" title={p.narrative}>
                    {p.narrative}
                  </TableCell>
                  <TableCell className="px-3 py-2 text-right">
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
