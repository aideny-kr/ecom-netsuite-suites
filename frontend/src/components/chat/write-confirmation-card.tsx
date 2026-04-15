"use client";

import { Plus, Pencil, Trash2, AlertTriangle, Check, X } from "lucide-react";
import type { WriteConfirmationData } from "@/lib/types";
import { cn } from "@/lib/utils";

interface WriteConfirmationCardProps {
  data: WriteConfirmationData;
  onConfirm: () => void;
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
  const MutationIcon = MUTATION_ICONS[data.mutation_type];
  const isPending = data.status === "pending";
  const isApproved = data.status === "approved";
  const isRejected = data.status === "rejected";

  const visibleProposedFields = Object.entries(data.proposed_fields).filter(
    ([key]) => !METADATA_KEYS.has(key),
  );

  const isUpdate = data.mutation_type === "update" || data.mutation_type === "upsert";
  const hasDiff = isUpdate && data.current_record !== null;

  return (
    <div
      className={cn(
        "rounded-xl border p-4 space-y-3 transition-colors",
        isPending && "border-amber-400/60 bg-amber-500/[0.02]",
        isApproved && "border-emerald-500/60 bg-emerald-500/[0.02]",
        isRejected && "border-red-400/60 bg-red-500/[0.02]",
      )}
    >
      {/* Header */}
      <div className="flex items-center gap-2">
        <div
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-lg",
            isPending && "bg-amber-500/10",
            isApproved && "bg-emerald-500/10",
            isRejected && "bg-red-500/10",
          )}
        >
          {isPending && <AlertTriangle className="h-4 w-4 text-amber-500" />}
          {isApproved && <Check className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />}
          {isRejected && <X className="h-4 w-4 text-red-500" />}
        </div>

        <div className="flex items-center gap-1.5">
          <MutationIcon
            className={cn(
              "h-3.5 w-3.5",
              isPending && "text-amber-600 dark:text-amber-400",
              isApproved && "text-emerald-600 dark:text-emerald-400",
              isRejected && "text-red-500",
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
        </div>
      </div>

      {/* Content */}
      {hasDiff ? (
        <DiffTable
          proposedFields={visibleProposedFields}
          currentRecord={data.current_record!}
        />
      ) : (
        <FieldsTable fields={visibleProposedFields} />
      )}

      {/* Action Buttons */}
      {isPending && (
        <div className="flex items-center gap-2 pt-1">
          <button
            type="button"
            onClick={onConfirm}
            disabled={disabled}
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
    </div>
  );
}

function FieldsTable({ fields }: { fields: [string, unknown][] }) {
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
                {formatValue(value)}
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
}: {
  proposedFields: [string, unknown][];
  currentRecord: Record<string, unknown>;
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
                  {formatValue(newValue)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
