"use client";

import { useEffect, useState } from "react";

import { AlertCircle, Check, HelpCircle, Minus } from "lucide-react";

import { cn } from "@/lib/utils";
import type { ClarificationData, ClarificationOption } from "@/lib/types";

interface Props {
  data: ClarificationData;
  onChoose: (optionId: "A" | "B" | "C") => void;
  expired?: boolean;
  disabled?: boolean;
}

const SOURCE_LABEL: Record<string, string> = {
  netsuite: "NetSuite",
  bigquery: "BigQuery",
  shopify: "Shopify",
  stripe: "Stripe",
  drive: "Drive",
};

export function ClarificationCard({
  data,
  onChoose,
  expired = false,
  disabled = false,
}: Props) {
  const [pendingPick, setPendingPick] = useState<string | null>(null);
  const isPending = data.status === "pending" && !expired;
  const isChosen = data.status === "chosen";
  const isSuperseded = data.status === "superseded";

  useEffect(() => {
    if (!isPending) return;

    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const inInput =
        target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA");
      if (inInput) return;

      const k = e.key;
      if (k === "Enter") {
        handlePick(data.default_id);
      } else if (k === "A" || k === "B" || k === "C") {
        if (data.options.some((o) => o.id === k)) {
          handlePick(k as "A" | "B" | "C");
        }
      } else if (k === "a" || k === "b" || k === "c") {
        const id = k.toUpperCase() as "A" | "B" | "C";
        if (data.options.some((o) => o.id === id)) {
          handlePick(id);
        }
      }
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPending, data.default_id, data.options.map((o) => o.id).join(",")]);

  function handlePick(id: "A" | "B" | "C") {
    if (disabled || pendingPick) return;
    setPendingPick(id);
    onChoose(id);
  }

  if (expired) {
    return (
      <div
        className="rounded-xl border border-red-400/60 bg-red-500/[0.02] p-4 space-y-2 animate-fade-in"
        role="region"
        aria-labelledby="clarify-expired-title"
      >
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-red-500/10">
            <AlertCircle className="h-4 w-4 text-red-500" />
          </div>
          <h3
            id="clarify-expired-title"
            className="text-[13px] font-semibold text-foreground"
          >
            This card expired
          </h3>
        </div>
        <p className="text-[12px] text-muted-foreground">
          Ask your question again to get a fresh card.
        </p>
      </div>
    );
  }

  if (isChosen) {
    const chosen = data.options.find((o) => o.id === data.chosen_id);
    if (!chosen) return null;
    return (
      <div
        className="rounded-xl border border-emerald-500/60 bg-emerald-500/[0.02] p-4 space-y-1 animate-fade-in"
        role="region"
      >
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-emerald-500/10">
            <Check className="h-4 w-4 text-emerald-600" />
          </div>
          <h3 className="text-[13px] font-semibold text-foreground">
            {chosen.title}
          </h3>
          <span className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-emerald-700 bg-emerald-500/10">
            chosen
          </span>
        </div>
        <p className="text-[12px] text-emerald-700/90 pl-9">
          {chosen.rationale} · {SOURCE_LABEL[chosen.source] ?? chosen.source}
        </p>
      </div>
    );
  }

  if (isSuperseded) {
    return (
      <div
        className="rounded-xl border border-border bg-muted/40 p-4 opacity-60 animate-fade-in"
        role="region"
      >
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-muted">
            <Minus className="h-4 w-4 text-muted-foreground" />
          </div>
          <h3 className="text-[13px] font-semibold text-muted-foreground">
            Replaced by your follow-up
          </h3>
        </div>
      </div>
    );
  }

  // Pending state
  return (
    <div
      className="rounded-xl border border-amber-400/60 bg-amber-500/[0.02] p-4 space-y-3 animate-fade-in"
      role="region"
      aria-labelledby={`clarify-${data.confirmation_token.slice(0, 8)}-title`}
    >
      <div className="flex items-center gap-2">
        <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-amber-500/10">
          <HelpCircle className="h-4 w-4 text-amber-600" />
        </div>
        <h3
          id={`clarify-${data.confirmation_token.slice(0, 8)}-title`}
          className="text-[13px] font-semibold text-foreground"
        >
          Pick a definition
        </h3>
        <span
          className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-amber-700 bg-amber-500/10"
          aria-live="polite"
        >
          awaiting your pick
        </span>
      </div>

      <p className="text-[13px] italic text-muted-foreground leading-relaxed">
        {data.ambiguity_summary}
      </p>

      <div
        className="flex flex-col gap-2"
        role="radiogroup"
        aria-label="clarification options"
      >
        {data.options.map((opt) => (
          <OptionButton
            key={opt.id}
            option={opt}
            isDefault={opt.id === data.default_id}
            isPending={pendingPick === opt.id}
            disabled={disabled || pendingPick !== null}
            onPick={() => handlePick(opt.id)}
          />
        ))}
      </div>

      <details className="group">
        <summary className="text-[12px] text-muted-foreground cursor-pointer select-none">
          Why these options?
        </summary>
        <div className="text-[12px] text-muted-foreground pt-2 pl-3 space-y-1">
          {data.options.map((opt) => (
            <p key={opt.id}>
              <span className="font-medium">{opt.id}.</span> {opt.title}: {opt.rationale}
            </p>
          ))}
        </div>
      </details>

      <p className="text-[11px] text-muted-foreground leading-relaxed">
        Or just type your answer.
      </p>
    </div>
  );
}

function OptionButton({
  option,
  isDefault,
  isPending,
  disabled,
  onPick,
}: {
  option: ClarificationOption;
  isDefault: boolean;
  isPending: boolean;
  disabled: boolean;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={isDefault}
      onClick={onPick}
      disabled={disabled}
      className={cn(
        "flex items-start gap-3 w-full px-3 py-2.5 rounded-lg text-left transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/40 focus-visible:ring-offset-2",
        "disabled:cursor-not-allowed disabled:opacity-60",
        isDefault
          ? "bg-emerald-600 text-white border border-emerald-700 hover:bg-emerald-700"
          : "bg-background text-foreground border border-border hover:bg-muted/40",
        isPending && "ring-2 ring-emerald-500/60"
      )}
    >
      <span
        className={cn(
          "flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full border text-[11px] font-bold mt-0.5",
          isDefault
            ? "border-white/50 bg-white/15"
            : "border-muted-foreground/40"
        )}
      >
        {option.id}
      </span>
      <span className="flex-1 min-w-0">
        <span className="block text-[13px] font-semibold leading-snug">
          {option.title}
          {isDefault && (
            <span className="ml-1.5 text-[10px] font-medium opacity-90">
              Recommended
            </span>
          )}
        </span>
        <span
          className={cn(
            "block text-[12px] leading-snug mt-0.5",
            isDefault ? "text-white/85" : "text-muted-foreground"
          )}
        >
          {option.rationale}
        </span>
      </span>
      <span
        className={cn(
          "inline-flex items-center text-[10px] font-medium rounded px-1.5 py-0.5 mt-0.5 shrink-0",
          isDefault
            ? "bg-white/20 text-white"
            : "bg-zinc-100 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-400"
        )}
      >
        {SOURCE_LABEL[option.source] ?? option.source}
      </span>
    </button>
  );
}
