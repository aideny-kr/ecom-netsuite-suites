"use client";

/**
 * Task 16 — the step inspector (mockup screen 3's `.insp`): a header (kind ·
 * adaptor, then the step's own title), Facts / Filter / Mapping / Scripts /
 * Errors tabs, all read straight off `detail`/`step` plus this flow's own
 * `useCeligoFlowErrors`. Read-only surface: nothing here runs, edits,
 * retries, resolves or syncs anything — "Open source" only hands `script_id`
 * to `onOpenScript` (Task 17's drawer), it never renders script `content`
 * itself (N2 — see the Scripts tab's shield banner, whose copy is the exact
 * string Global Constraints mandates, not the shorter one the mockup
 * sketched for this pane).
 *
 * `step: null` (nothing selected) keeps the Task 14 stub's own contract —
 * `data-testid="celigo-step-inspector-stub"` with "no step selected" in its
 * text — because `celigo-flow-page.test.tsx` (Task 14, not owned by this
 * task) already asserts exactly that against the real `CeligoFlowPage`, and
 * the flow's own Overview (AI description, sync freshness) already lives in
 * `celigo-flow-header.tsx` for that state, so this pane has nothing further
 * to add when no step is selected.
 */

import {
  useCeligoFlowErrors,
  type CeligoFlowDetail,
  type CeligoFlowErrorGroup,
  type CeligoFlowStep,
} from "@/hooks/use-celigo-flows";
import { queryState } from "@/lib/query-state";
import { countRules } from "./chips";
import { ErrorNotice, N2_SHIELD_TEXT, fallbackStepTitle, formatRelativeTime } from "./shared";
import { FilterPanel, FieldMappingPanel } from "./inspector-panels";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Loader2, ShieldAlert } from "lucide-react";

export type InspectorTab = "facts" | "filter" | "mapping" | "scripts" | "errors";

const KIND_LABEL: Record<CeligoFlowStep["kind"], string> = {
  source: "Source",
  lookup: "Lookup",
  destination: "Destination",
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** First 8 hex chars + an ellipsis -- the truncated-id convention already
 * live on this surface (`step-bubble.tsx`'s `factLine`, the mockup's
 * `648bd44c…`/`66738c3d…`). Connection and celigo-id NAMES never come
 * through the sync (a standing, named gap — see the plan's Deferred
 * table), so every id renders this way; there is no resolved-name path to
 * fall back to. */
function truncateId(id: string): string {
  return `${id.slice(0, 8)}…`;
}

/** "17 Aug 06:20" -- day, short month, UTC time, read off UTC fields so a
 * viewer's timezone never changes what a fixed historical timestamp says
 * (same reasoning as `celigo-flow-header.tsx`'s `formatFullDate`). `null`/
 * unparsable is "—". */
function formatSignatureTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "—";
  const d = new Date(ms);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${hh}:${mm}`;
}

/** "16 Sep" -- day + short month only, no time or year: the purge date is a
 * calendar date Celigo enforces, not a moment. `null` is "—". */
function formatPurgeDate(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "—";
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

/** "33.3 KB" off `script_size_chars` (a character count, the only size unit
 * Task 8's sync carries — there is no byte length recorded). `null` (not
 * synced) returns `null` so the caller can omit the fact entirely rather
 * than print a fabricated "0 KB". */
function formatScriptSize(chars: number | null): string | null {
  if (chars == null) return null;
  return `${(chars / 1024).toFixed(1)} KB`;
}

/** The step's position among Celigo's own router/branch/sequence facts --
 * `lookup · router 1 · branch 1 · step 1 of 1`. Router numbering is the
 * flow's OWN declared `routers` array order (never a re-sort — the same
 * numbering `router-node.tsx`/`celigo-flow-canvas.tsx` already establish for
 * the canvas's "Router N" labels); branch numbering is `branch.order + 1`
 * (the same field `celigo-flow-canvas.tsx`'s lane header reads for "Branch
 * N"). The step's own position within its branch is its rank by `sequence`
 * among every step sharing that exact `(router_id, branch_id)` pair — never
 * array order, which a paginated or out-of-order sync response need not
 * preserve. A step with no router at all (`router_id: null`, e.g. most flow
 * sources) simply omits the router/branch segments. */
function stepPositionLine(detail: CeligoFlowDetail, step: CeligoFlowStep): string {
  const parts = [step.kind as string];
  if (step.router_id) {
    const routerIndex = detail.routers.findIndex((r) => r.id === step.router_id);
    if (routerIndex !== -1) {
      parts.push(`router ${routerIndex + 1}`);
      const branch = detail.routers[routerIndex].branches.find((b) => b.id === step.branch_id);
      if (branch) parts.push(`branch ${branch.order + 1}`);
    }
  }
  const siblings = detail.steps
    .filter((s) => s.router_id === step.router_id && s.branch_id === step.branch_id)
    .sort((a, b) => a.sequence - b.sequence);
  const position = siblings.findIndex((s) => s.id === step.id);
  parts.push(`step ${position + 1} of ${siblings.length}`);
  return parts.join(" · ");
}

/** Whether this exact step object (by `celigo_id`) is wired into more than
 * one place in this flow -- Celigo can attach the same underlying step to
 * more than one branch, and a reader comparing two branches side by side
 * needs to know a step recurring in both isn't a coincidence of two
 * similarly-configured steps but the literal same one. Only ever answers
 * for THIS flow (the data this component already has); a step reused across
 * OTHER flows entirely is outside what `detail` can say. Names only the
 * FIRST other occurrence found -- the two-branch case the mockup and every
 * live flow that reuses a step actually shows. A step attached in three or
 * more branches would still read as "also in Branch N" for one of them,
 * naming a real branch rather than every one; the plan does not ask for an
 * exhaustive list here, and no live flow does this today. */
function usedElsewhereLine(detail: CeligoFlowDetail, step: CeligoFlowStep): string {
  const other = detail.steps.find((s) => s.id !== step.id && s.celigo_id === step.celigo_id);
  if (!other) return "only in this flow";
  const router = detail.routers.find((r) => r.id === other.router_id);
  const branch = router?.branches.find((b) => b.id === other.branch_id);
  return branch ? `also in Branch ${branch.order + 1}` : "also used elsewhere in this flow";
}

function FactRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-2 py-1 text-[12px]">
      <span className="w-24 shrink-0 text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className={mono ? "font-mono" : undefined}>{value}</span>
    </div>
  );
}

function FactsTab({ detail, step }: { detail: CeligoFlowDetail; step: CeligoFlowStep }) {
  return (
    <div className="flex flex-col">
      {/* `|| "—"`, not `?? "—"`: an empty adaptor_type is an absence, and the
          header above already reads it that way (`step.adaptor_type ? …`).
          With `??` this row printed a blank where the dash belongs. */}
      <FactRow label="adaptor" value={step.adaptor_type || "—"} mono />
      <FactRow
        label="connection"
        value={step.connection_celigo_id ? `${truncateId(step.connection_celigo_id)} · name not synced` : "—"}
        mono
      />
      <FactRow label="role" value={stepPositionLine(detail, step)} />
      <FactRow label="celigo id" value={truncateId(step.celigo_id)} mono />
      <FactRow label="used elsewhere" value={usedElsewhereLine(detail, step)} />
    </div>
  );
}

type CeligoAttachmentLike = {
  script_copies_count: number | null;
  script_versions_count: number | null;
  script_version_letter: string | null;
};

/** `1 copy · 1 version` when this attachment's script isn't part of a
 * meaningful clone family (`script_copies_count <= 1`); `copy C of 3
 * versions · 7 copies` when it is -- the family form always names THIS
 * attachment's own copy letter first, since that's the answer to "which one
 * runs here" a reader actually needs. `null` (either count not synced) so
 * the caller can fall back to a "not synced" line instead of a fabricated
 * "1 copy". */
function scriptFamilyLine(att: CeligoAttachmentLike): string | null {
  if (att.script_copies_count == null || att.script_versions_count == null) return null;
  const versionsWord = `version${att.script_versions_count === 1 ? "" : "s"}`;
  if (att.script_copies_count <= 1) {
    return `${att.script_copies_count} copy · ${att.script_versions_count} ${versionsWord}`;
  }
  const letter = att.script_version_letter ?? "?";
  return `copy ${letter} of ${att.script_versions_count} ${versionsWord} · ${att.script_copies_count} copies`;
}

function ScriptSiteCard({
  attachment,
  onOpenScript,
}: {
  attachment: CeligoFlowStep["attachments"][number];
  onOpenScript: (scriptId: string) => void;
}) {
  const familyLine = scriptFamilyLine(attachment);
  const sizeLine = formatScriptSize(attachment.script_size_chars);
  return (
    <div className="rounded-lg border p-2.5 text-[12px]">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="outline" className="border-blue-500/50 bg-blue-500/10 text-[10px] text-blue-700 dark:text-blue-400">
          {/* `||`, not `??`: an empty function_name is "not recorded", the
              same as null -- "HK " with nothing after it names nothing. */}
          {`HK ${attachment.function_name || "hook"}`}
        </Badge>
        <span className="font-mono text-[12px]">{attachment.script_name ?? "name not synced"}</span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[11px] text-muted-foreground">
        <span>{familyLine ?? "family state not synced"}</span>
        {sizeLine && <span>{sizeLine}</span>}
      </div>
      <div className="mt-1 break-all font-mono text-[11px] text-muted-foreground">{attachment.json_path}</div>
      <div className="mt-1.5">
        {attachment.script_id ? (
          <button
            type="button"
            className="text-[12px] font-medium text-foreground underline"
            onClick={() => onOpenScript(attachment.script_id!)}
          >
            Open source →
          </button>
        ) : (
          <span className="text-[12px] text-muted-foreground">script body not synced</span>
        )}
      </div>
    </div>
  );
}

function ScriptsTab({
  step,
  onOpenScript,
}: {
  step: CeligoFlowStep;
  onOpenScript: (scriptId: string) => void;
}) {
  if (step.attachments.length === 0) {
    return <p className="text-[12px] text-muted-foreground">No scripts attached to this step.</p>;
  }
  return (
    <div className="flex flex-col gap-2.5">
      {step.attachments.map((att) => (
        <ScriptSiteCard key={att.id} attachment={att} onOpenScript={onOpenScript} />
      ))}
      <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-2.5">
        <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" aria-hidden />
        <p className="text-[11px] text-muted-foreground">{N2_SHIELD_TEXT}</p>
      </div>
    </div>
  );
}

function ErrorSignatureCard({ group }: { group: CeligoFlowErrorGroup }) {
  return (
    <div className="rounded-lg border p-2.5 text-[12px]">
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="border-red-500/50 bg-red-500/10 text-[11px] text-red-700 dark:text-red-400">
          {group.signature?.code ?? "unknown"}
        </Badge>
        <span className="font-mono text-[11px] text-muted-foreground">{group.signature?.source ?? "—"}</span>
        <span className="ml-auto tabular-nums">{`×${group.count}`}</span>
      </div>
      {group.signature?.sample_message && (
        <p className="mt-1 font-mono text-[12px]">{group.signature.sample_message}</p>
      )}
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[10.5px] text-muted-foreground">
        <span>{`first ${formatSignatureTimestamp(group.first_seen_at)}`}</span>
        <span>{`latest ${formatSignatureTimestamp(group.last_seen_at)}`}</span>
        {group.retriable === false && <span>not retriable</span>}
        {group.retriable === true && <span>retriable</span>}
        <span>{`purges ${formatPurgeDate(group.purge_at)}`}</span>
      </div>
      {group.trace_keys.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {group.trace_keys.map((key) => (
            <span key={key} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">
              {key}
            </span>
          ))}
        </div>
      )}
      {/* No "open these in recon" link (finding I7): these are Celigo
          integration errors, and the reconciliation surface neither knows
          about them nor lists them -- the link went to a page that could not
          answer for the number it named. The trace-key chips above are the
          real handle: an operator carries one into Celigo's own error view. */}
    </div>
  );
}

function ErrorsTab({
  errorsState,
  groups,
  onRetry,
  lastSyncedAt,
  flowErrorCount,
}: {
  errorsState: "pending" | "error" | "success";
  groups: CeligoFlowErrorGroup[];
  onRetry: () => void;
  lastSyncedAt: string | null;
  /** `detail.error_count` -- every open error on THIS FLOW, not just this
   * step's. The empty state below says two different things depending on it,
   * and neither may be guessed from `groups` alone: `groups` is already
   * filtered to the signatures touching this step, so it is empty in both
   * cases. */
  flowErrorCount: number;
}) {
  if (errorsState === "pending") {
    return (
      <div className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" />
        Loading errors…
      </div>
    );
  }
  if (errorsState === "error") {
    return <ErrorNotice message="Couldn't load errors for this step." onRetry={onRetry} />;
  }
  if (groups.length === 0) {
    // A step with no signatures of its own is not evidence that the FLOW is
    // clean. This used to print "Celigo reported 0 for the whole flow" in
    // both cases -- a flow-wide claim, stated on a flow with open errors
    // sitting in other steps. The quiet sentence is only honest when the
    // flow's own count is zero; otherwise it says where the errors are.
    return (
      <p className="text-[12px] text-muted-foreground">
        {flowErrorCount === 0
          ? `No open errors. Celigo reported 0 on the last sync, ${formatRelativeTime(lastSyncedAt)}.`
          : `No open errors on this step. ${flowErrorCount} open elsewhere in this flow as of the last sync, ${formatRelativeTime(lastSyncedAt)}.`}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2.5">
      {groups.map((group) => (
        <ErrorSignatureCard key={group.signature?.id ?? group.trace_keys.join(",")} group={group} />
      ))}
    </div>
  );
}

export function CeligoStepInspector({
  detail,
  step,
  tab,
  onTabChange,
  lastSyncedAt,
  onOpenScript,
}: {
  detail: CeligoFlowDetail;
  step: CeligoFlowStep | null;
  tab: InspectorTab;
  onTabChange: (tab: InspectorTab) => void;
  lastSyncedAt: string | null;
  onOpenScript: (scriptId: string) => void;
}): JSX.Element {
  const errorsQuery = useCeligoFlowErrors(detail.id);
  const errorsState = queryState(errorsQuery);

  if (!step) {
    return (
      <div
        data-testid="celigo-step-inspector-stub"
        data-tab={tab}
        className="flex h-full flex-col items-center justify-center p-4 text-center text-[12px] text-muted-foreground"
      >
        Inspector — no step selected. Select a step on the canvas to inspect it.
      </div>
    );
  }

  // The groups whose root cause TOUCHES this step -- the detail the Errors tab
  // body renders. Note a group's own `count` is flow-wide: one signature can
  // span several steps, which is exactly why the tab BADGE below reads
  // `step.error_count` (the backend's per-step attribution) instead of summing
  // these. Summing them showed a shared signature's whole total on every step
  // it touched (finding I3).
  const matchingGroups =
    errorsState === "success" ? (errorsQuery.data?.groups ?? []).filter((g) => g.step_ids.includes(step.id)) : [];
  const filterRuleCount = step.filter_json ? countRules(step.filter_json) : null;
  const mappingFieldCount =
    step.mapping_json &&
    typeof step.mapping_json === "object" &&
    !Array.isArray(step.mapping_json) &&
    Array.isArray((step.mapping_json as Record<string, unknown>).fields)
      ? ((step.mapping_json as { fields: unknown[] }).fields.length)
      : null;
  const title = step.reference_name ?? fallbackStepTitle(step).text;

  return (
    <div data-testid="celigo-step-inspector" className="flex h-full flex-col">
      <div className="border-b px-3 py-2.5">
        <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
          {`${KIND_LABEL[step.kind]}${step.adaptor_type ? ` · ${step.adaptor_type}` : ""}`}
        </p>
        <p className="mt-0.5 text-[13px] font-semibold leading-snug">{title}</p>
      </div>
      <Tabs
        value={tab}
        onValueChange={(v) => onTabChange(v as InspectorTab)}
        className="flex flex-1 min-h-0 flex-col"
      >
        <TabsList className="mx-3 mt-2 h-auto self-start">
          <TabsTrigger value="facts" className="text-[12px]">
            Facts
          </TabsTrigger>
          <TabsTrigger value="filter" className="text-[12px]">
            {`Filter ${filterRuleCount === null ? "—" : filterRuleCount}`}
          </TabsTrigger>
          <TabsTrigger value="mapping" className="text-[12px]">
            {`Mapping ${mappingFieldCount === null ? "—" : mappingFieldCount}`}
          </TabsTrigger>
          <TabsTrigger value="scripts" className="text-[12px]">
            {`Scripts ${step.attachments.length}`}
          </TabsTrigger>
          <TabsTrigger value="errors" className="text-[12px]">
            {/* `step.error_count` came with `detail`, so this badge is a settled
                number the moment the step is selected -- it no longer waits on
                (or degrades to "…" with) the separate errors query. */}
            {`Errors ${step.error_count}`}
          </TabsTrigger>
        </TabsList>
        <div className="flex-1 min-h-0 overflow-auto p-3">
          <TabsContent value="facts" className="mt-0">
            <FactsTab detail={detail} step={step} />
          </TabsContent>
          <TabsContent value="filter" className="mt-0">
            {step.filter_json ? (
              <FilterPanel step={step} />
            ) : (
              <p className="text-[12px] text-muted-foreground">No filter on this step</p>
            )}
          </TabsContent>
          <TabsContent value="mapping" className="mt-0">
            {step.mapping_json ? (
              <FieldMappingPanel step={step} />
            ) : (
              <p className="text-[12px] text-muted-foreground">No response mapping on this step</p>
            )}
          </TabsContent>
          <TabsContent value="scripts" className="mt-0">
            <ScriptsTab step={step} onOpenScript={onOpenScript} />
          </TabsContent>
          <TabsContent value="errors" className="mt-0">
            <ErrorsTab
              errorsState={errorsState}
              groups={matchingGroups}
              onRetry={() => errorsQuery.refetch()}
              lastSyncedAt={lastSyncedAt}
              flowErrorCount={detail.error_count}
            />
          </TabsContent>
        </div>
      </Tabs>
    </div>
  );
}
