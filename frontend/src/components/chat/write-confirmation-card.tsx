"use client";

import { useState } from "react";
import { Plus, Pencil, Trash2, AlertTriangle, Check, X, Loader2 } from "lucide-react";
import type { WriteConfirmationData, EditableSlot } from "@/lib/types";
import { cn } from "@/lib/utils";

interface WriteConfirmationCardProps {
  data: WriteConfirmationData;
  onConfirm: (slotValues: Record<string, string>) => void;
  onReject: () => void;
  disabled?: boolean;
}

const MUTATION_ICONS = {
  create: Plus,
  update: Pencil,
  delete: Trash2,
  upsert: Pencil,
} as const;

const METADATA_KEYS = new Set(["id", "type"]);

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function toTitleCase(str: string): string {
  return str
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function WriteConfirmationCard({
  data,
  onConfirm,
  onReject,
  disabled = false,
}: WriteConfirmationCardProps) {
  const [slotValues, setSlotValues] = useState<Record<string, string>>({});

  const MutationIcon = MUTATION_ICONS[data.mutation_type];
  const isPending = data.status === "pending";
  const isExecuting = data.status === "executing";
  const isApproved = data.status === "approved";
  const isRejected = data.status === "rejected";
  const isFailed = data.status === "failed";
  // A write whose outcome NetSuite never confirmed — a timeout or an
  // unreadable response. Deliberately NOT folded into isFailed: the
  // failed panel asserts "Nothing was written", which on 2026-08-27 was
  // untrue (customer 5264348 existed) and is the sentence that sends an
  // operator off to create a duplicate.
  const isIndeterminate = data.status === "indeterminate";

  const slots = data.editable_slots ?? [];
  const unfillableLineFields = data.unfillable_line_fields ?? [];
  const invariantErrors = data.invariant_errors ?? [];
  // Non-empty unfillable_line_fields makes the card terminal (operator
  // decision, plan Task 9): a half-form the human can fill in and approve,
  // which then fails at NetSuite anyway, is worse than an honest stop. Line-
  // level slots are deferred — ClickUp 86bbgznjr.
  const isLineTerminal = unfillableLineFields.length > 0;
  // Non-empty invariant_errors means the server CHECKED this payload against
  // a posting invariant (debits=credits, period open) and it failed — same
  // terminal standing as unfillable_line_fields (operator ruling, MF-1: "we
  // checked and it IS wrong" carries the same weight as a missing required
  // field). Kept as its own flag rather than folded into isLineTerminal so
  // both panels can render independently when both are present.
  const hasInvariantErrors = invariantErrors.length > 0;
  const isTerminal = isLineTerminal || hasInvariantErrors;
  // A slot-fill form only renders when there IS something fillable and the
  // card isn't already terminal. editable_slots: [] (the recon-group-
  // confirmation shape, and a fully-resolved payload) never reaches this —
  // those cards render exactly as they did before this.
  const showSlotForm = isPending && slots.length > 0 && !isTerminal;
  // .trim() — a space is not a value. Without it a human can type a single
  // space into a required slot, watch Approve light up, and believe they
  // supplied the field when they didn't. The server would reject an
  // untrimmed value against its allowlist anyway (compares via `str(value)`),
  // so accepting whitespace here only buys a confusing server-side rejection.
  const allSlotsFilled = slots.every((slot) => (slotValues[slot.name] ?? "").trim() !== "");

  const visibleProposedFields = Object.entries(data.proposed_fields).filter(
    ([key]) => !METADATA_KEYS.has(key),
  );
  const proposedLines = data.proposed_lines ?? [];

  const isUpdate = data.mutation_type === "update" || data.mutation_type === "upsert";
  const hasDiff = isUpdate && data.current_record !== null;

  // No badge for a plain pending card — that's the recon-group-confirmation
  // shape (and any fully-resolved payload) and must keep rendering exactly
  // as it does today.
  const pendingBadgeLabel = hasInvariantErrors
    ? "Blocked"
    : isLineTerminal
      ? "Incomplete"
      : showSlotForm
        ? "Needs input"
        : data.unvalidated
          ? "Unvalidated"
          : null;

  function handleSlotChange(name: string, value: string) {
    setSlotValues((prev) => ({ ...prev, [name]: value }));
  }

  // Trim on send, not just on the filled-check: what reaches onConfirm (and
  // from there write_confirm.slot_values) must be what the card told the
  // human it captured — never "  2  " silently mismatching a trimmed check.
  function handleApprove() {
    const trimmed = Object.fromEntries(
      Object.entries(slotValues).map(([name, value]) => [name, value.trim()]),
    );
    onConfirm(trimmed);
  }

  return (
    <div
      className={cn(
        "rounded-xl border p-4 space-y-3 transition-colors",
        isPending && "border-amber-400/60 bg-amber-500/[0.02]",
        isExecuting && "border-blue-400/60 bg-blue-500/[0.02]",
        isApproved && "border-emerald-500/60 bg-emerald-500/[0.02]",
        isRejected && "border-red-400/60 bg-red-500/[0.02]",
        isFailed && "border-red-400/60 bg-red-500/[0.02]",
        isIndeterminate && "border-amber-400/60 bg-amber-500/[0.02]",
      )}
    >
      {/* Header */}
      <div className="flex items-center gap-2">
        <div
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-lg",
            isPending && "bg-amber-500/10",
            isExecuting && "bg-blue-500/10",
            isApproved && "bg-emerald-500/10",
            (isRejected || isFailed) && "bg-red-500/10",
            isIndeterminate && "bg-amber-500/10",
          )}
        >
          {isPending && <AlertTriangle className="h-4 w-4 text-amber-500" />}
          {isExecuting && <Loader2 className="h-4 w-4 animate-spin text-blue-500" />}
          {isApproved && <Check className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />}
          {(isRejected || isFailed) && <X className="h-4 w-4 text-red-500" />}
          {isIndeterminate && <AlertTriangle className="h-4 w-4 text-amber-500" />}
        </div>

        <div className="flex items-center gap-1.5">
          <MutationIcon
            className={cn(
              "h-3.5 w-3.5",
              isPending && "text-amber-600 dark:text-amber-400",
              isExecuting && "text-blue-600 dark:text-blue-400",
              isApproved && "text-emerald-600 dark:text-emerald-400",
              (isRejected || isFailed) && "text-red-500",
              isIndeterminate && "text-amber-600 dark:text-amber-400",
            )}
          />
          <span className="text-[13px] font-semibold text-foreground">
            {toTitleCase(data.mutation_type)} {data.record_type}
          </span>
          {data.record_id && (
            <span className="text-[11px] text-muted-foreground">#{data.record_id}</span>
          )}
        </div>

        <div className="ml-auto">
          {isExecuting && (
            <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-blue-700 bg-blue-500/10 dark:text-blue-400">
              <Loader2 className="h-3 w-3 animate-spin" />
              Writing to NetSuite…
            </span>
          )}
          {isApproved && (
            <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-emerald-700 bg-emerald-500/10 dark:text-emerald-400">
              <Check className="h-3 w-3" />
              Approved
            </span>
          )}
          {isRejected && (
            <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-red-600 bg-red-500/10 dark:text-red-400">
              <X className="h-3 w-3" />
              Cancelled
            </span>
          )}
          {isFailed && (
            <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-red-600 bg-red-500/10 dark:text-red-400">
              <X className="h-3 w-3" />
              Failed
            </span>
          )}
          {isIndeterminate && (
            <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-amber-700 bg-amber-500/10 dark:text-amber-400">
              <AlertTriangle className="h-3 w-3" />
              Unconfirmed
            </span>
          )}
          {isPending && pendingBadgeLabel && (
            <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-amber-700 bg-amber-500/10 dark:text-amber-400">
              <AlertTriangle className="h-3 w-3" />
              {pendingBadgeLabel}
            </span>
          )}
        </div>
      </div>

      {/* Content */}
      {data.target_environment && (
        /* WHERE this write lands. A tenant can hold both a sandbox and a
           production NetSuite connector, and the operator approving this card
           is the last gate — they must be able to see which books it hits.
           Production is styled as a warning, sandbox as neutral: the point is
           that "this is real" reads differently from "this is a rehearsal". */
        <div
          className={cn(
            "flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-[12px]",
            data.target_environment === "PRODUCTION"
              ? "border-amber-500/50 bg-amber-500/[0.06] text-amber-700 dark:text-amber-400"
              : "border-border/60 bg-muted/30 text-muted-foreground",
          )}
        >
          <span className="font-semibold uppercase tracking-wide">
            {data.target_environment}
          </span>
          {data.target_account && <span className="font-mono">{data.target_account}</span>}
        </div>
      )}

      {hasDiff ? (
        <DiffTable
          proposedFields={visibleProposedFields}
          currentRecord={data.current_record!}
          fieldLabels={data.field_labels}
        />
      ) : (
        <FieldsTable fields={visibleProposedFields} fieldLabels={data.field_labels} />
      )}

      {proposedLines.length > 0 && <LinesTable lines={proposedLines} />}

      {isPending && data.unvalidated && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-400/40 bg-amber-500/[0.04] p-2.5">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">
              Could not be validated
            </p>
            <p className="mt-1 text-[12px] leading-snug text-muted-foreground">
              NetSuite&apos;s field requirements were unavailable, so this payload was not checked.
              Review every value before approving.
            </p>
          </div>
        </div>
      )}

      {isIndeterminate && (
        <div className="rounded-lg border border-amber-400/40 bg-amber-500/[0.04] p-2.5">
          <p className="text-[12px] font-medium text-amber-700 dark:text-amber-400">
            This {data.mutation_type} may or may not have completed
          </p>
          <p className="mt-1 text-[12px] leading-snug text-muted-foreground">
            NetSuite never confirmed the result
            {data.error ? ` (${data.error})` : ""}. That is not the same as a rejection — the{" "}
            {data.record_type} may already exist.
          </p>
          <p className="mt-2 text-[11px] italic text-muted-foreground">
            Check the {data.record_type} in NetSuite before trying again. Re-running this now risks
            creating it twice.
          </p>
        </div>
      )}

      {isFailed && data.error && (
        <div className="rounded-lg border border-red-400/40 bg-red-500/[0.04] p-2.5">
          <p className="text-[12px] font-medium text-red-600 dark:text-red-400">
            NetSuite rejected this write
          </p>
          <p className="mt-1 text-[12px] leading-snug text-muted-foreground">{data.error}</p>
          <p className="mt-2 text-[11px] italic text-muted-foreground">
            Nothing was written. Ask again to propose a corrected write.
          </p>
        </div>
      )}

      {isPending && hasInvariantErrors && (
        <div className="space-y-1 rounded-lg border border-amber-400/40 bg-amber-500/[0.04] p-3">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">
            Posting rules violated
          </p>
          {invariantErrors.map((message) => (
            <p key={message} className="text-[12px] leading-snug text-muted-foreground">
              {message}
            </p>
          ))}
          <p className="text-[12px] leading-snug text-muted-foreground">
            NetSuite would reject this write. Reject it here and ask again with a corrected payload.
          </p>
        </div>
      )}

      {isPending && isLineTerminal && (
        <div className="space-y-1 rounded-lg border border-amber-400/40 bg-amber-500/[0.04] p-3">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">
            Line items need attention
          </p>
          <p className="text-[12px] leading-snug text-muted-foreground">
            These line-item fields are missing and can&apos;t be filled in here:{" "}
            {unfillableLineFields.join(", ")}. Reject this write and ask again with the missing
            details.
          </p>
        </div>
      )}

      {showSlotForm && (
        <div className="space-y-2 rounded-lg border border-amber-400/40 bg-amber-500/[0.04] p-3">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">
            Required — please complete
          </p>
          {slots.map((slot) => (
            <SlotField
              key={slot.name}
              slot={slot}
              value={slotValues[slot.name] ?? ""}
              onChange={(value) => handleSlotChange(slot.name, value)}
            />
          ))}
        </div>
      )}

      {/* In-flight: claim won, write is executing against NetSuite. No
          Approve/Reject — the claim already took the row past the point
          where either action makes sense. */}
      {isExecuting && (
        <div className="flex items-center gap-2 pt-1">
          <span className="flex items-center gap-1.5 text-[11px] italic text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
            This may take a few seconds.
          </span>
        </div>
      )}

      {/* Action Buttons */}
      {isPending && !isTerminal && (
        <div className="flex items-center gap-2 pt-1">
          {showSlotForm && !allSlotsFilled && (
            <span className="mr-auto text-[11px] italic text-muted-foreground">
              Approve stays disabled until every field is filled
            </span>
          )}
          <button
            type="button"
            onClick={handleApprove}
            disabled={disabled || (showSlotForm && !allSlotsFilled)}
            className={cn(
              "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors",
              "bg-emerald-600 text-white hover:bg-emerald-700",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            <Check className="h-3.5 w-3.5" />
            Approve
          </button>
          <button
            type="button"
            onClick={onReject}
            disabled={disabled}
            className={cn(
              "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors",
              "border border-border bg-background hover:bg-muted text-muted-foreground",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            <X className="h-3.5 w-3.5" />
            Reject
          </button>
        </div>
      )}

      {isPending && isTerminal && (
        <div className="flex items-center gap-2 pt-1">
          <span className="mr-auto text-[11px] italic text-muted-foreground">
            This write can&apos;t be completed here.
          </span>
          <button
            type="button"
            onClick={onReject}
            disabled={disabled}
            className={cn(
              "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors",
              "border border-border bg-background hover:bg-muted text-muted-foreground",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            <X className="h-3.5 w-3.5" />
            Reject
          </button>
        </div>
      )}
    </div>
  );
}

function SlotField({
  slot,
  value,
  onChange,
}: {
  slot: EditableSlot;
  value: string;
  onChange: (value: string) => void;
}) {
  const inputId = `slot-${slot.name}`;
  return (
    <div className="flex items-center gap-3">
      <label htmlFor={inputId} className="w-40 shrink-0 text-[12px] text-muted-foreground">
        {slot.label}
      </label>
      {slot.allowed && slot.allowed.length > 0 ? (
        <select
          id={inputId}
          className="h-8 flex-1 rounded-md border bg-background px-2 text-[13px]"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">Select…</option>
          {slot.allowed.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      ) : (
        <input
          id={inputId}
          type="text"
          className="h-8 flex-1 rounded-md border bg-background px-2 text-[13px]"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}

function FieldsTable({
  fields,
  fieldLabels,
}: {
  fields: [string, unknown][];
  fieldLabels?: Record<string, string>;
}) {
  if (fields.length === 0) return null;
  return (
    <div className="overflow-hidden rounded-lg border border-border/50">
      <table className="w-full text-[13px]">
        <tbody>
          {fields.map(([key, value], idx) => (
            <tr
              key={key}
              className={cn(
                "border-b border-border/30 last:border-0",
                idx % 2 === 0 ? "bg-background" : "bg-muted/20",
              )}
            >
              <td className="px-3 py-2 text-[12px] font-medium text-muted-foreground w-1/3 align-top">
                {key}
              </td>
              <td className="px-3 py-2 text-[13px] text-foreground break-all">
                {/* A NetSuite reference arrives as {"id":"5"}; the server
                    resolves a human label for it where it can. Fall back to
                    the raw value so an unresolved id is still shown rather
                    than hidden. */}
                {fieldLabels?.[key] ?? formatValue(value)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DiffTable({
  proposedFields,
  currentRecord,
  fieldLabels,
}: {
  proposedFields: [string, unknown][];
  currentRecord: Record<string, unknown>;
  fieldLabels?: Record<string, string>;
}) {
  if (proposedFields.length === 0) return null;
  return (
    <div className="overflow-hidden rounded-lg border border-border/50">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b border-border/30 bg-muted/30">
            <th className="px-3 py-1.5 text-left text-[11px] font-medium text-muted-foreground w-1/3">
              Field
            </th>
            <th className="px-3 py-1.5 text-left text-[11px] font-medium text-red-500 dark:text-red-400 w-1/3">
              Before
            </th>
            <th className="px-3 py-1.5 text-left text-[11px] font-medium text-emerald-600 dark:text-emerald-400 w-1/3">
              After
            </th>
          </tr>
        </thead>
        <tbody>
          {proposedFields.map(([key, newValue], idx) => {
            const oldValue = currentRecord[key];
            return (
              <tr
                key={key}
                className={cn(
                  "border-b border-border/30 last:border-0",
                  idx % 2 === 0 ? "bg-background" : "bg-muted/20",
                )}
              >
                <td className="px-3 py-2 text-[12px] font-medium text-muted-foreground align-top">
                  {key}
                </td>
                <td className="px-3 py-2 text-[13px] text-red-600 dark:text-red-400 line-through break-all align-top">
                  {formatValue(oldValue)}
                </td>
                <td className="px-3 py-2 text-[13px] text-emerald-700 dark:text-emerald-400 break-all align-top">
                  {fieldLabels?.[key] ?? formatValue(newValue)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function LinesTable({ lines }: { lines: Record<string, unknown>[] }) {
  const columns = Array.from(
    lines.reduce((set, line) => {
      Object.keys(line).forEach((key) => set.add(key));
      return set;
    }, new Set<string>()),
  );
  return (
    <div className="space-y-1">
      <p className="text-[11px] font-medium text-muted-foreground">Line items</p>
      <div className="overflow-x-auto rounded-lg border border-border/50">
        <table className="w-full text-[12px]">
          <thead>
            <tr className="border-b border-border/30 bg-muted/30">
              {columns.map((col) => (
                <th
                  key={col}
                  className="px-2 py-1 text-left text-[11px] font-medium text-muted-foreground"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {lines.map((line, idx) => (
              <tr
                key={idx}
                className={cn(
                  "border-b border-border/30 last:border-0",
                  idx % 2 === 0 ? "bg-background" : "bg-muted/20",
                )}
              >
                {columns.map((col) => (
                  <td key={col} className="px-2 py-1.5 align-top text-foreground break-all">
                    {formatValue(line[col])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
