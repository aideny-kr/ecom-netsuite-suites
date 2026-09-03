"use client";

/**
 * Task 15 — one flow-step bubble on the canvas (mockup screen 3's
 * `.bubble`): eyebrow (Celigo's own kind vocabulary + app family), the title
 * (the synced `reference_name` when Celigo has it, else the honest fallback
 * from Task 8's `fallbackStepTitle` — never an invented name), the fact
 * line (adaptor/operation/record-type, a saved-search id, or a bare
 * connection id when nothing more specific synced), Task 8's
 * `affordanceChips`, and a footer stating what happens on failure
 * (lookups/destinations) or whether retries are on (sources).
 *
 * Read-only: every click here only SELECTS a step — the bubble itself picks
 * the Facts tab (no `tab` arg; `celigo-flow-page.tsx` defaults it), a chip
 * click jumps straight to the tab that chip's own data lives on. Nothing
 * here runs, edits, retries, or syncs anything.
 */

import type { CeligoFlowStep } from "@/hooks/use-celigo-flows";
import type { LayoutNode } from "./layout";
import type { InspectorTab } from "./celigo-step-inspector";
import { affordanceChips, type Chip } from "./chips";
import { adaptorFamily, fallbackStepTitle } from "./shared";
import { cn } from "@/lib/utils";

const APP_GLYPH: Record<string, string> = {
  NetSuite: "N",
  HTTP: "H",
  AS2: "A",
  FTP: "F",
  RDBMS: "D",
  REST: "R",
};

const KIND_LABEL: Record<CeligoFlowStep["kind"], string> = {
  source: "Source",
  lookup: "Lookup",
  destination: "Destination",
};

const KIND_BORDER: Record<CeligoFlowStep["kind"], string> = {
  source: "border-l-green-600",
  lookup: "border-l-teal-600",
  destination: "border-l-blue-600",
};

/** The fact line under the title — adaptor/operation/record-type for a
 * NetSuite destination, the saved-search id for a NetSuite lookup, or a
 * generic `{family} {export|import} · conn {first 8 chars}…` for everything
 * else (HTTP, AS2, FTP, RDBMS, REST — none of these carry enough synced
 * structure to say more). `Import`/`Export` reads off the adaptor type
 * string itself, which is why an HTTP LOOKUP still says "http export" —
 * Celigo's own HTTPExport adaptor backs both a flow's source and its
 * lookups; the word describes the adaptor, not the step's role. */
function factLine(step: CeligoFlowStep): string {
  const family = adaptorFamily(step.adaptor_type);
  if (family === "NetSuite") {
    if (step.kind === "lookup" && step.search_id) {
      return `export · saved search ${step.search_id}${step.record_type ? ` · ${step.record_type}` : ""}`;
    }
    if (step.operation && step.record_type) {
      return `import · ${step.operation} · ${step.record_type}`;
    }
  }
  const word = step.adaptor_type?.includes("Import") ? "import" : "export";
  const conn = step.connection_celigo_id ? ` · conn ${step.connection_celigo_id.slice(0, 8)}…` : "";
  return `${(family ?? "HTTP").toLowerCase()} ${word}${conn}`;
}

/** Where a chip click sends the inspector — the tab that chip's OWN data
 * actually renders on (Task 16), never a made-up destination. Hooks and
 * transform both live under Scripts (a source's "no transform" is still a
 * scripting fact); the two filter slots share Filter; the two mapping slots
 * share Mapping. */
function tabForChip(slot: Chip["slot"]): InspectorTab {
  switch (slot) {
    case "hooks":
    case "transform":
      return "scripts";
    case "input_filter":
    case "output_filter":
      return "filter";
    case "ns_mapping":
    case "response_mapping":
    default:
      return "mapping";
  }
}

type ChipTone = "tr" | "hk" | "fl" | "mp" | "none" | "unsynced";

const CHIP_TONE_CLASSES: Record<ChipTone, string> = {
  tr: "bg-violet-500/10 text-violet-700 dark:text-violet-400",
  hk: "bg-blue-500/10 text-blue-700 dark:text-blue-400",
  fl: "bg-teal-500/10 text-teal-700 dark:text-teal-400",
  mp: "bg-zinc-500/10 text-zinc-600 dark:text-zinc-400",
  none: "border border-border text-muted-foreground",
  unsynced: "border border-dashed border-border text-muted-foreground",
};

const SLOT_TONE: Record<Chip["slot"], ChipTone> = {
  transform: "tr",
  hooks: "hk",
  output_filter: "fl",
  input_filter: "fl",
  ns_mapping: "mp",
  response_mapping: "mp",
};

function ChipButton({ chip, onClick }: { chip: Chip; onClick: () => void }): JSX.Element {
  const tone = chip.state === "configured" ? SLOT_TONE[chip.slot] : chip.state;
  const isHook = chip.slot === "hooks" && chip.state === "configured";
  const versionBadge = isHook && chip.copiesCount && chip.copiesCount > 1 ? `${chip.versionLetter ?? "?"}/${chip.versionsCount ?? "?"}` : "×1";
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-px text-[9px] leading-tight",
        CHIP_TONE_CLASSES[tone],
      )}
    >
      {isHook ? (
        <>
          {"HK "}
          <span className="font-mono">{chip.functionName}</span>
          <span className="rounded border border-current px-[3px] text-[8.5px]">{versionBadge}</span>
          {chip.diverged && (
            <span aria-hidden title="copies of this script differ" className="h-[5px] w-[5px] rounded-full bg-amber-500" />
          )}
        </>
      ) : (
        chip.label
      )}
    </button>
  );
}

/** Destinations/lookups (`role === "processor"`) state what happens on
 * failure; sources (`role === "generator"`) state whether retries are on —
 * the two fields Celigo actually carries per role. */
function footerText(step: CeligoFlowStep): { text: string; amber: boolean } {
  if (step.role === "generator") {
    return step.skip_retries === true ? { text: "retries skipped", amber: false } : { text: "retries on", amber: false };
  }
  if (step.proceed_on_failure === false) return { text: "stops flow on failure", amber: false };
  if (step.proceed_on_failure === true) return { text: "continues on failure", amber: true };
  return { text: "stops on failure · default", amber: false };
}

export function StepBubble({
  step,
  node,
  selected,
  paused,
  onSelect,
}: {
  step: CeligoFlowStep;
  node: Pick<LayoutNode, "x" | "y" | "w" | "h">;
  selected: boolean;
  paused: boolean;
  onSelect: (stepId: string, tab?: InspectorTab) => void;
}): JSX.Element {
  const family = adaptorFamily(step.adaptor_type);
  const fallback = fallbackStepTitle(step);
  const title = step.reference_name ?? fallback.text;
  const unsynced = !step.reference_name && fallback.unsynced;
  const chips = affordanceChips(step);
  const footer = footerText(step);
  const hasError = step.error_count > 0;

  return (
    <div
      data-testid={`step-bubble-${step.id}`}
      data-selected={selected ? "true" : undefined}
      data-error={hasError ? "true" : undefined}
      onClick={() => onSelect(step.id, undefined)}
      style={{ position: "absolute", left: node.x, top: node.y, width: node.w, height: node.h }}
      className={cn(
        "flex cursor-pointer flex-col gap-1 rounded-xl border border-l-[3px] bg-card p-2 text-[11px] shadow-soft",
        KIND_BORDER[step.kind],
        selected && "ring-2 ring-orange-500",
        paused && "opacity-60",
        hasError && "border-red-500/50",
      )}
    >
      <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-wide text-muted-foreground">
        <span className="flex h-[15px] w-[15px] shrink-0 items-center justify-center rounded bg-foreground text-[8.5px] font-bold text-card">
          {family ? APP_GLYPH[family] ?? family[0] : "?"}
        </span>
        <span>{family ? `${KIND_LABEL[step.kind]} · ${family}` : KIND_LABEL[step.kind]}</span>
        {hasError && (
          <span className="ml-auto text-[10px] normal-case tracking-normal text-red-600 dark:text-red-400">{`${step.error_count} open`}</span>
        )}
      </div>
      <div
        data-unsynced={unsynced ? "true" : undefined}
        className={cn("min-h-[31px] text-[12.5px] font-semibold leading-snug", unsynced && "font-medium text-muted-foreground")}
      >
        {title}
      </div>
      <div className="truncate font-mono text-[10px] text-muted-foreground">{factLine(step)}</div>
      <div className="mt-px flex flex-wrap gap-[3px]">
        {chips.map((chip) => (
          <ChipButton key={chip.slot} chip={chip} onClick={() => onSelect(step.id, tabForChip(chip.slot))} />
        ))}
      </div>
      <div className={cn("mt-auto truncate text-[9.5px] text-muted-foreground", footer.amber && "text-amber-600 dark:text-amber-400")}>
        {footer.text}
      </div>
    </div>
  );
}
