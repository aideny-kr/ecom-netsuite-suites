"use client";

import { useState } from "react";
import {
  AlertTriangle,
  Check,
  Plus,
  Pencil,
  Trash2,
  X,
  Rocket,
} from "lucide-react";
import type { DeployPreview } from "@/lib/types";
import { cn } from "@/lib/utils";

interface DeployConfirmationCardProps {
  preview: DeployPreview;
  onConfirm: () => void;
  onCancel: () => void;
  disabled?: boolean;
}

const OP_ICONS = {
  create: Plus,
  modify: Pencil,
  delete: Trash2,
  unchanged: null,
} as const;

const OP_STYLES: Record<string, string> = {
  create: "text-emerald-600 dark:text-emerald-400",
  modify: "text-amber-600 dark:text-amber-400",
  delete: "text-red-600 dark:text-red-400",
  unchanged: "text-muted-foreground",
};

const GATE_STYLES: Record<string, string> = {
  passed: "text-emerald-600 dark:text-emerald-400",
  failed: "text-red-600 dark:text-red-400",
  not_required: "text-muted-foreground",
  missing: "text-amber-600 dark:text-amber-400",
};

const INITIAL_VISIBLE = 5;

export function DeployConfirmationCard({
  preview,
  onConfirm,
  onCancel,
  disabled = false,
}: DeployConfirmationCardProps) {
  const [expanded, setExpanded] = useState(false);

  const touchedFiles = preview.manifest.filter(
    (entry) => entry.operation !== "unchanged",
  );
  const visible = expanded
    ? preview.manifest
    : preview.manifest.slice(0, INITIAL_VISIBLE);
  const hidden = preview.manifest.length - visible.length;

  return (
    <div
      role="region"
      aria-label="Sandbox deploy confirmation"
      data-testid="deploy-confirmation-card"
      className={cn(
        "rounded-xl border border-amber-400/60 bg-amber-500/[0.02] p-3 space-y-3",
      )}
    >
      <div className="flex items-center gap-2">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-amber-500/10">
          <AlertTriangle className="h-4 w-4 text-amber-500" />
        </div>
        <div className="flex items-center gap-1.5">
          <Rocket className="h-3.5 w-3.5 text-amber-600 dark:text-amber-400" />
          <span className="text-[13px] font-semibold text-foreground">
            Deploy {touchedFiles.length} file
            {touchedFiles.length === 1 ? "" : "s"} to{" "}
            <span className="font-mono">{preview.sandbox_id}</span>?
          </span>
        </div>
      </div>

      {/* Gates */}
      <div className="rounded-md border border-border/50 px-3 py-2 text-[12px]">
        <p className="text-[10px] font-medium uppercase text-muted-foreground mb-1">
          Gates
        </p>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span>
            validate:{" "}
            <span className={GATE_STYLES[preview.gates.validate.status] ?? ""}>
              {preview.gates.validate.status}
            </span>
          </span>
          <span>
            tests:{" "}
            <span className={GATE_STYLES[preview.gates.unit_tests.status] ?? ""}>
              {preview.gates.unit_tests.status}
            </span>
          </span>
          <span>
            assertions:{" "}
            <span
              className={GATE_STYLES[preview.gates.assertions.status] ?? ""}
            >
              {preview.gates.assertions.status}
            </span>
          </span>
        </div>
      </div>

      {/* File list */}
      <div className="rounded-md border border-border/50 px-3 py-2">
        <p className="text-[10px] font-medium uppercase text-muted-foreground mb-1">
          Files
        </p>
        <ul className="space-y-1 text-[12px] font-mono">
          {visible.map((entry) => {
            const Icon = OP_ICONS[entry.operation];
            return (
              <li key={entry.path} className="flex items-center gap-2">
                <span className={cn("inline-block w-3", OP_STYLES[entry.operation])}>
                  {Icon ? (
                    <Icon className="h-3 w-3" />
                  ) : (
                    <span className="text-muted-foreground">·</span>
                  )}
                </span>
                <span className="truncate">{entry.path}</span>
              </li>
            );
          })}
        </ul>
        {hidden > 0 && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            data-testid="deploy-show-more-files"
            className="mt-1 text-[11px] text-muted-foreground underline"
          >
            … show {hidden} more
          </button>
        )}
      </div>

      {/* Sandbox + expiry meta */}
      <p className="text-[11px] text-muted-foreground">
        This action runs <code>suitecloud project:deploy</code> against{" "}
        <span className="font-mono">{preview.sandbox_id}</span> and cannot be
        undone. Preview expires{" "}
        {new Date(preview.expires_at).toLocaleTimeString()}.
      </p>

      <div className="flex items-center gap-2 pt-1">
        <button
          type="button"
          onClick={onConfirm}
          disabled={disabled}
          data-testid="deploy-confirm-button"
          className={cn(
            "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors",
            "bg-emerald-600 text-white hover:bg-emerald-700",
            "disabled:cursor-not-allowed disabled:opacity-50",
          )}
        >
          <Check className="h-3.5 w-3.5" />
          Confirm Deploy
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={disabled}
          data-testid="deploy-cancel-button"
          className={cn(
            "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors",
            "border border-border bg-background hover:bg-muted text-muted-foreground",
            "disabled:cursor-not-allowed disabled:opacity-50",
          )}
        >
          <X className="h-3.5 w-3.5" />
          Cancel
        </button>
      </div>
    </div>
  );
}
