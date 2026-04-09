"use client";

import { Database, BarChart3, Check } from "lucide-react";
import type { SourcePickerData, SourcePickerOption } from "@/lib/types";
import { cn } from "@/lib/utils";

interface SourcePickerCardProps {
  data: SourcePickerData;
  onPick: (source: "netsuite" | "bigquery") => void;
  disabled?: boolean;
}

const OPTION_ICONS = {
  netsuite: Database,
  bigquery: BarChart3,
} as const;

export function SourcePickerCard({ data, onPick, disabled = false }: SourcePickerCardProps) {
  const selected = data.selected ?? null;
  const hasSelection = selected !== null;
  return (
    <div className="mt-1 space-y-3">
      <div className="text-[13px] text-muted-foreground">
        {hasSelection
          ? "I'll use this source for the rest of the chat."
          : "I can answer this from either source. Which would you like me to use?"}
      </div>
      <div className="text-[13px] font-medium text-foreground">{data.user_question}</div>
      {!hasSelection && (
        <div className="text-[12px] italic text-muted-foreground/70">{data.reason}</div>
      )}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {data.options.map((option) => (
          <SourceOption
            key={option.source}
            option={option}
            onClick={() => onPick(option.source)}
            disabled={disabled || hasSelection}
            isSelected={selected === option.source}
            isDimmed={hasSelection && selected !== option.source}
          />
        ))}
      </div>
    </div>
  );
}

function SourceOption({
  option,
  onClick,
  disabled,
  isSelected,
  isDimmed,
}: {
  option: SourcePickerOption;
  onClick: () => void;
  disabled: boolean;
  isSelected: boolean;
  isDimmed: boolean;
}) {
  const Icon = OPTION_ICONS[option.source];
  return (
    <div
      className={cn(
        "rounded-xl border bg-card p-4 shadow-soft transition-all",
        isSelected
          ? "border-emerald-500/60 ring-2 ring-emerald-500/20 bg-emerald-500/[0.03]"
          : option.recommended
            ? "border-[var(--chat-accent)]/40 ring-1 ring-[var(--chat-accent)]/10"
            : "border-border/50",
        isDimmed && "opacity-40",
      )}
    >
      {isSelected ? (
        <div className="mb-2 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-emerald-700 bg-emerald-500/10 dark:text-emerald-400">
          <Check className="h-3 w-3" />
          Selected
        </div>
      ) : option.recommended ? (
        <div className="mb-2 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-[var(--chat-accent)] bg-[var(--chat-accent-glow)]">
          Recommended
        </div>
      ) : null}
      <div className="flex items-center gap-2">
        <div
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
            isSelected ? "bg-emerald-500/10" : "bg-primary/10",
          )}
        >
          <Icon
            className={cn("h-4 w-4", isSelected ? "text-emerald-600 dark:text-emerald-400" : "text-primary")}
          />
        </div>
        <div className="text-[14px] font-semibold text-foreground">{option.label}</div>
      </div>
      <p className="mt-2 text-[12px] text-muted-foreground">{option.description}</p>
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        className={cn(
          "mt-3 w-full rounded-md px-3 py-2 text-[13px] font-medium transition-colors",
          "border border-border/60 bg-background/60 hover:bg-background",
          "disabled:cursor-not-allowed disabled:opacity-50",
        )}
      >
        {isSelected ? "Using" : "Use"} {option.label}
      </button>
    </div>
  );
}
