/**
 * Task 8 — shared formatters and small presentational pieces for the Celigo
 * flow pages (schedule.ts's `ParsedSchedule`/`StallState` render here). Two
 * of these (`formatRelativeTime`, `ErrorNotice`) intentionally REPRODUCE —
 * not import — the private helpers of the same name in
 * `components/settings/celigo-flow-map.tsx`: that file is deleted in Task
 * 18 once the new pages replace it, so importing from it would be a
 * dependency on code with a known expiry date. `formatRelativeTime` here
 * also differs in shape (accepts `now` for deterministic tests, and adds
 * months/years buckets the old one never needed) — a genuinely different
 * function that happens to solve the same problem, not a copy-paste.
 */
import type { CeligoFlowDetail, CeligoFlowStep } from "@/hooks/use-celigo-flows";
import type { ParsedSchedule, StallState } from "./schedule";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// N2_SHIELD_TEXT — the one N2 banner string, defined once.
// ---------------------------------------------------------------------------

/** Global Constraints' exact N2 banner copy, verbatim -- not the shorter
 * "Source opens in a drawer…" line the mockup sketched for the inspector's
 * Scripts pane. The binding rule names one canonical string project-wide, so
 * it is DEFINED once here and imported by every surface that shows customer
 * JavaScript (`celigo-step-inspector.tsx`'s Scripts tab and
 * `settings/celigo-script-viewer.tsx`'s banner). It used to be a hand-kept
 * duplicate in both files, on the reasoning that a copy is safer than an
 * import that outlives its usefulness -- but two copies of a mandated string
 * is exactly the shape that drifts: an edit to one leaves the other stating
 * a different promise about the same content, with nothing to catch it. */
export const N2_SHIELD_TEXT =
  "Customer-authored JavaScript, shown to you only. Never run here, never sent to the assistant.";

// ---------------------------------------------------------------------------
// formatRelativeTime
// ---------------------------------------------------------------------------

/** "2 min ago" / "3 h ago" / "6 days ago" / "17 months ago" / "3 years ago";
 * `null` (no timestamp at all -- never synced, or a query still pending
 * upstream) is "—", never a fabricated "just now". `now` defaults to the
 * real clock but is a parameter so a test can freeze it -- this function
 * must never read `Date.now()` implicitly, or every call site becomes
 * un-testable without faking global time. */
export function formatRelativeTime(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const diffMs = now.getTime() - then;
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} day${days === 1 ? "" : "s"} ago`;
  const months = Math.floor(days / 30);
  if (months < 24) return `${months} month${months === 1 ? "" : "s"} ago`;
  const years = Math.floor(days / 365);
  return `${years} year${years === 1 ? "" : "s"} ago`;
}

// ---------------------------------------------------------------------------
// adaptorFamily
// ---------------------------------------------------------------------------

/** Groups Celigo's specific adaptor type strings (`HTTPExport`,
 * `NetSuiteDistributedImport`, `AS2Import`, …) into the app-family medallion
 * shown on tiles/bubbles. Checked in a fixed order so a compound name (were
 * one ever to exist) resolves to the more specific family first; `null` (no
 * adaptor recorded) stays `null` rather than guessing. */
export function adaptorFamily(adaptorType: string | null): "NetSuite" | "HTTP" | "AS2" | "FTP" | "RDBMS" | "REST" | null {
  if (!adaptorType) return null;
  if (adaptorType.includes("NetSuite")) return "NetSuite";
  if (adaptorType.includes("AS2")) return "AS2";
  if (adaptorType.includes("FTP")) return "FTP";
  if (adaptorType.includes("RDBMS")) return "RDBMS";
  if (adaptorType.includes("REST")) return "REST";
  if (adaptorType.includes("HTTP")) return "HTTP";
  return null;
}

// ---------------------------------------------------------------------------
// fallbackStepTitle
// ---------------------------------------------------------------------------

/** The step's own Celigo name isn't synced yet (a known, named gap — see
 * the plan), so every bubble title is honestly derived from what the step
 * DOES rather than an invented name. NetSuite carries enough structured
 * fact to say something confident: `"add salesorder"` for a destination,
 * `"lookup customer · search 5090"` for a saved-search lookup. Everything
 * else (HTTP, AS2, FTP, RDBMS, REST, or a NetSuite step missing the field
 * that would make the confident form possible) falls back to a generic
 * `"{family} {export|lookup|destination} · name not synced"` — `unsynced:
 * true` for that generic case AND for the NetSuite lookup (its title
 * depends on a search id, not a description of what it returns, so it
 * still reads as "cannot say more" -- matching the mockup's `.unsynced`
 * styling on both), `false` only for the NetSuite destination case, which
 * states a real fact outright.
 *
 * When the ADAPTOR itself isn't synced (or is a string this client doesn't
 * recognise), there is no family word to use and the title says so:
 * `"Destination · adaptor not synced"`. It used to substitute a literal
 * `"HTTP"` there — inventing a specific, plausible app family out of an
 * absence, which is precisely what every other line of this function exists
 * to avoid. A guess that reads like a fact is worse than a stated gap. */
export function fallbackStepTitle(
  step: Pick<CeligoFlowStep, "kind" | "adaptor_type" | "record_type" | "operation" | "search_id">,
): { text: string; unsynced: boolean } {
  const family = adaptorFamily(step.adaptor_type);
  if (family === "NetSuite") {
    if (step.kind === "destination" && step.operation && step.record_type) {
      return { text: `${step.operation} ${step.record_type}`, unsynced: false };
    }
    if (step.kind === "lookup" && step.search_id) {
      return { text: `lookup ${step.record_type ?? "record"} · search ${step.search_id}`, unsynced: true };
    }
  }
  if (!family) {
    return { text: `${KIND_WORD[step.kind]} · ${ADAPTOR_NOT_SYNCED}`, unsynced: true };
  }
  const kindWord = step.kind === "source" ? "export" : step.kind === "lookup" ? "lookup" : "destination";
  return { text: `${family} ${kindWord} · name not synced`, unsynced: true };
}

/** The one phrase for "Celigo gave us no adaptor for this step" — used by the
 * fallback title above and by `step-bubble.tsx`'s fact line, which state the
 * same absence in two places on the same bubble. */
export const ADAPTOR_NOT_SYNCED = "adaptor not synced";

/** Celigo's own kind vocabulary, capitalised — the same three words
 * `step-bubble.tsx` and `celigo-step-inspector.tsx` already print as their
 * eyebrow/header label. */
const KIND_WORD: Record<CeligoFlowStep["kind"], string> = {
  source: "Source",
  lookup: "Lookup",
  destination: "Destination",
};

// ---------------------------------------------------------------------------
// deriveFlowSummary
// ---------------------------------------------------------------------------

/** NetSuite record types that don't read as English words split apart —
 * enough to make the derived summary readable for the flows this surface
 * actually writes to (see CLAUDE.md's NetSuite record-type vocabulary).
 * Anything not in here renders as its raw record_type, which is still a
 * real word for most custom record types. */
const RECORD_TYPE_DISPLAY: Record<string, string> = {
  salesorder: "sales order",
  itemfulfillment: "item fulfillment",
  itemreceipt: "item receipt",
  customerdeposit: "customer deposit",
  customerrefund: "customer refund",
  purchaseorder: "purchase order",
  returnauthorization: "return authorization",
  transferorder: "transfer order",
  inventoryadjustment: "inventory adjustment",
};

function recordTypeDisplay(recordType: string): string {
  return RECORD_TYPE_DISPLAY[recordType] ?? recordType;
}

/** Turns an ordered run of destination/lookup steps into Celigo's own
 * "looks up the customer, adds it, updates it, then adds the sales order"
 * phrasing: a step repeating the PREVIOUS step's record type reads as "it";
 * a step introducing a new one is spelled out ("the sales order"). The last
 * phrase (when there's more than one) gets a "then" — matching the pattern
 * live in the connector spec's own Multi-Subsidiary flow. */
function branchVerbPhrase(steps: CeligoFlowStep[]): string {
  let lastRecordType: string | null = null;
  const phrases = steps.map((s) => {
    const sameType = s.record_type !== null && s.record_type === lastRecordType;
    const noun = sameType ? "it" : `the ${s.record_type ? recordTypeDisplay(s.record_type) : "record"}`;
    if (s.record_type) lastRecordType = s.record_type;
    if (s.kind === "lookup") return `looks up ${noun}`;
    const verb = s.operation === "add" ? "adds" : s.operation === "update" ? "updates" : s.operation ? `${s.operation}s` : "processes";
    return `${verb} ${noun}`;
  });
  if (phrases.length > 1) phrases[phrases.length - 1] = `then ${phrases[phrases.length - 1]}`;
  return phrases.join(", ");
}

/** A one-sentence, computed (never LLM-authored) description of what a flow
 * does, built entirely off its own steps/routers — never a hardcoded flow
 * name or a mockup number. Falls back to a bare shape count when there's no
 * source step to anchor on (an empty flow, or one whose steps haven't
 * synced kinds yet) rather than guessing at a sentence with nothing to
 * hang it on. */
/** "NetSuite", "NetSuite and FTP", "NetSuite, FTP and HTTP" — an ordinary
 * English list, deduped, in the order the sources appear. A source whose
 * adaptor didn't sync contributes "an unsynced adaptor" rather than being
 * dropped: silently omitting it would under-report how many places this flow
 * pulls from, which is the same class of error as inventing one. */
function joinSourceNames(names: string[]): string {
  const unique = Array.from(new Set(names));
  if (unique.length === 1) return unique[0];
  return `${unique.slice(0, -1).join(", ")} and ${unique[unique.length - 1]}`;
}

export function deriveFlowSummary(detail: CeligoFlowDetail): string {
  const sources = detail.steps.filter((s) => s.kind === "source");
  const source = sources[0];
  if (!source) {
    return `${detail.steps.length} step${detail.steps.length === 1 ? "" : "s"} · ${detail.routers.length} router${detail.routers.length === 1 ? "" : "s"}`;
  }

  // Every source, not just the first one found. A flow pulling from both
  // NetSuite and an FTP drop used to read as if it had a single NetSuite
  // source — a summary that quietly halved the flow's own inputs.
  const sourceNames = joinSourceNames(sources.map((s) => adaptorFamily(s.adaptor_type) ?? "an unsynced adaptor"));
  const sourcePhrase = source.record_type ? recordTypeDisplay(source.record_type) : "records";
  let sentence = `Gets ${sourcePhrase} from ${sourceNames}`;

  const preRouteLookups = detail.steps.filter((s) => s.kind === "lookup" && !s.branch_id);
  if (preRouteLookups.length > 0) sentence += " → looks each one up again";

  const routingRouter = detail.routers.find((r) => r.branches.length > 1);
  let branchSteps: CeligoFlowStep[];
  if (routingRouter) {
    // A branch with no id cannot be told apart from the router's OTHER
    // id-less branches, so "the first branch's steps" was really "every
    // unattributed step of this router" — described as if it were one
    // branch's pipeline. With ids missing, the honest answer is the count
    // and nothing more.
    if (routingRouter.branches.some((b) => b.id === null)) {
      return `${sentence} → routes to ${routingRouter.branches.length} branches (branch ids not synced).`;
    }
    sentence += ` → routes on ${routingRouter.branches.length} branches`;
    const firstBranch = routingRouter.branches[0];
    branchSteps = detail.steps
      .filter((s) => s.router_id === routingRouter.id && s.branch_id === firstBranch.id)
      .sort((a, b) => a.sequence - b.sequence);
  } else {
    const preRouteIds = new Set(preRouteLookups.map((s) => s.id));
    branchSteps = detail.steps
      .filter((s) => s.kind !== "source" && !preRouteIds.has(s.id))
      .sort((a, b) => a.sequence - b.sequence);
  }
  if (branchSteps.length > 0) sentence += ` → per branch: ${branchVerbPhrase(branchSteps)}`;
  return `${sentence}.`;
}

// ---------------------------------------------------------------------------
// ErrorNotice — an errored query must never render as loading or empty.
// ---------------------------------------------------------------------------

/** Every `useCeligo*` call site gates on `queryState(query)` — `"error"`
 * renders THIS, never silently as `"pending"` (a spinner with no escape) or
 * `"success"` with empty data (a misleading "0 flows"). Reproduced from
 * `celigo-flow-map.tsx`'s private helper of the same name/shape (see this
 * file's top docstring for why it's a reproduction, not an import). */
export function ErrorNotice({ message, onRetry }: { message: string; onRetry?: () => void }): JSX.Element {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/5 px-3 py-2 text-[13px] text-destructive">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      <span className="flex-1">{message}</span>
      {onRetry && (
        <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pill — the one pill primitive every status chip on these pages builds on.
// ---------------------------------------------------------------------------

const PILL_TONE_CLASSES: Record<"ok" | "crit" | "warn" | "mute" | "accent", string> = {
  ok: "border-green-500/50 bg-green-500/10 text-green-700 dark:text-green-400",
  crit: "border-red-500/50 bg-red-500/10 text-red-700 dark:text-red-400",
  warn: "border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400",
  mute: "border-border bg-muted text-muted-foreground",
  accent: "border-orange-500/50 bg-orange-500/10 text-orange-700 dark:text-orange-400",
};

export function Pill({
  tone,
  dot,
  children,
  title,
}: {
  tone: "ok" | "crit" | "warn" | "mute" | "accent";
  dot?: "solid" | "hollow";
  children: React.ReactNode;
  title?: string;
}): JSX.Element {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-[10.5px] leading-tight",
        PILL_TONE_CLASSES[tone],
      )}
    >
      {dot && (
        <span
          aria-hidden
          className={cn("h-1.5 w-1.5 shrink-0 rounded-full", dot === "solid" ? "bg-current" : "border border-current bg-transparent")}
        />
      )}
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// ErrorPill — the error fact, with the timestamp it was checked at.
// ---------------------------------------------------------------------------

/** "0 open errors · checked 4 min ago" when clean — a zero is a claim with
 * a timestamp, never a bare decoration. "10 open · 1 root cause" when not —
 * root cause count leads (the actionable number), raw error count is the
 * headline figure next to it, never buried behind it. */
export function ErrorPill({
  count,
  signatureCount,
  checkedAt,
}: {
  count: number;
  signatureCount?: number;
  checkedAt: string | null;
}): JSX.Element {
  if (count === 0) {
    return (
      <Pill tone="ok" dot="solid">
        0 open errors <span className="opacity-70">· checked {formatRelativeTime(checkedAt)}</span>
      </Pill>
    );
  }
  const sig = signatureCount ?? count;
  return (
    <Pill tone="crit" dot="solid">
      {count} open · {sig} root cause{sig === 1 ? "" : "s"}
    </Pill>
  );
}

// ---------------------------------------------------------------------------
// SchedulePill — the schedule/stall fact, the pill that would catch a
// scheduled flow that quietly stopped (absence is not success).
// ---------------------------------------------------------------------------

export function SchedulePill({ stall, parsed }: { stall: StallState; parsed: ParsedSchedule }): JSX.Element {
  switch (stall.state) {
    case "on_time":
      return (
        <Pill tone="ok" dot="solid">
          on time
        </Pill>
      );
    case "stalled":
      return (
        <Pill tone="warn" dot="solid">
          stalled? {stall.missedRuns} run{stall.missedRuns === 1 ? "" : "s"} missed
        </Pill>
      );
    case "paused":
      return (
        // "paused", not "all paused": every caller of this pill is a SINGLE
        // flow (the flow header, and one row of the flows table), where "all"
        // reads as a claim about a set that isn't there. The integrations
        // dashboard has its own `AttentionPill`, which says "all paused" and
        // means it -- every flow in the integration.
        <Pill tone="mute" dot="hollow">
          paused
        </Pill>
      );
    case "on_demand":
      return (
        <Pill tone="mute" dot="hollow">
          on demand only
        </Pill>
      );
    case "no_run":
      return (
        <Pill tone="mute" dot="hollow">
          no run recorded
        </Pill>
      );
    default:
      return (
        <Pill tone="mute" dot="hollow" title={parsed.kind === "unknown" ? parsed.raw : undefined}>
          —
        </Pill>
      );
  }
}

// ---------------------------------------------------------------------------
// Medallions — the app-family badges on a tile/table row.
// ---------------------------------------------------------------------------

const MEDAL: Record<string, { code: string; className: string }> = {
  NetSuite: { code: "NS", className: "bg-[#1F3B6C]" },
  HTTP: { code: "HTTP", className: "bg-[#4B5563]" },
  AS2: { code: "AS2", className: "bg-[#7C3AED]" },
  FTP: { code: "FTP", className: "bg-[#0D9488]" },
  RDBMS: { code: "DB", className: "bg-[#B45309]" },
  REST: { code: "REST", className: "bg-[#52525B]" },
};

/** Renders in the order given — callers pass `adaptor_families`, which the
 * API already returns alphabetically, so this never re-sorts. An unknown
 * family (a shape this client hasn't seen) still renders, badged with its
 * own name, rather than silently dropping an app off the tile. */
export function Medallions({ families }: { families: string[] }): JSX.Element {
  return (
    <span className="inline-flex gap-[3px]">
      {families.map((family) => {
        const medal = MEDAL[family];
        return (
          <span
            key={family}
            className={cn(
              "inline-flex h-4 min-w-[16px] items-center justify-center rounded px-1 text-[8.5px] font-bold tracking-wide text-white",
              medal?.className ?? "bg-foreground",
            )}
          >
            {medal?.code ?? family}
          </span>
        );
      })}
    </span>
  );
}
