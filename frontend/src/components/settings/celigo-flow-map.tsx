"use client";

/**
 * Task 9 — flow map (mockup screen 02) + flow detail (screen 03), built
 * against Task 8's real read-only endpoints (`use-celigo-flows.ts`).
 *
 * THREE deviations from the mockup, not two -- the plan named the first two;
 * the third is forced by what Task 8 actually returns (see task-9-report.md):
 *
 * 1. (plan-authorized) The error pill leads with SIGNATURE count ("N root
 *    causes"), raw error count secondary -- root causes are the actionable
 *    number, volume is context.
 * 2. (plan-authorized) A paused (`disabled`) flow stays in the tree, dimmed
 *    with a "Paused" pill -- never filtered out.
 * 3. (forced by reality, then partially undone) The mockup's "Last synced"
 *    stat and "Sync now" button were BOTH dropped at first: Task 8 exposed
 *    no endpoint for either, and a button with no working action is worse
 *    than no button. Fix round 1 added `GET /celigo/sync-status` (Task 8
 *    follow-up), so "Last synced" is wired in below (`formatRelativeTime` +
 *    the 4th `StatCard`). "Sync now" is STILL dropped -- no trigger endpoint
 *    exists yet.
 *
 * Fix round 1 also added the rule this file now applies everywhere it calls
 * a `useCeligo*` hook: an `isError` query state must render as a VISIBLY
 * DIFFERENT state from "loading" or "genuinely empty" -- never silently as
 * either. See `ErrorNotice` and its call sites for why (a misleading empty
 * state is worse than a blank one; a stuck spinner has no escape).
 *
 * Every other field choice below is grounded in Task 8's response shape or
 * in `sanitizer.py`'s CONFIRMED-live Celigo schemas (schedule, filter,
 * responseMapping) -- see the comments at each formatter for the specific
 * evidence. Nothing here hardcodes a mockup NUMBER, flow name, or schedule.
 */

import { useState } from "react";
import {
  useCeligoAllFlows,
  useCeligoFlowDetail,
  useCeligoIntegrations,
  useCeligoSyncStatus,
  type CeligoAttachment,
  type CeligoFlowStep,
  type CeligoFlowSummary,
  type CeligoIntegration,
} from "@/hooks/use-celigo-flows";
import { CeligoScriptViewerDialog } from "@/components/settings/celigo-script-viewer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { AlertTriangle, ChevronDown, ChevronRight, Loader2 } from "lucide-react";

/** Task 10 wiring: a step/node's script badge opens the script viewer for
 * the FIRST attachment that resolved to a local script row -- `script_id`
 * is nullable (an attachment can reference a `script_celigo_id` that never
 * synced as its own `CeligoScript` row). A step with multiple distinct
 * scripts attached opens only the first; the viewer's own attachment table
 * still shows every site for that script's clone family, but not sibling
 * scripts on the same step -- a known scope limit, not an oversight. */
function firstOpenableScriptId(attachments: CeligoAttachment[]): string | null {
  return attachments.find((a) => a.script_id)?.script_id ?? null;
}

// ---------------------------------------------------------------------------
// Formatters -- each grounded in a confirmed real shape, not the mockup.
// ---------------------------------------------------------------------------

/** Only `{type:"everyN", unit, value}` is CONFIRMED live (Task 8's own
 * fixture, `backend/tests/api/test_celigo_flows_api.py`'s `_seed_world`).
 * Any other schedule shape Celigo can send is real but unverified here --
 * falls back to a generic label rather than inventing a display string
 * (e.g. the mockup's ":05, :35") for a shape nobody has confirmed. */
export function formatSchedule(schedule: Record<string, unknown> | string | null): string {
  if (!schedule) return "on demand";
  // The shape actually observed live (2026-09-01, Framework: 96 of 239
  // flows): Celigo's own cron string. Shown verbatim -- it is the real
  // configuration, and any prettified rendering of it would be a claim this
  // code cannot verify against Celigo's scheduler semantics.
  if (typeof schedule === "string") return schedule.trim() || "on demand";
  if (Object.keys(schedule).length === 0) return "on demand";
  if (schedule.type === "everyN" && typeof schedule.value === "number" && typeof schedule.unit === "string") {
    return `every ${schedule.value} ${schedule.unit}`;
  }
  return "custom schedule";
}

/** `last_synced_at` is `cursor_states`' freshness cursor -- a real ISO
 * timestamp (`GET /celigo/sync-status`, Task 8), never a fabricated
 * value. `null` covers "no connection" and "connected but never synced"
 * identically (see `CeligoSyncStatus`'s docstring) -- surfaced by the
 * caller, not here, since only the caller knows whether the query itself
 * is still loading/errored (this formatter only handles the resolved
 * value). */
function formatRelativeTime(iso: string | null): string {
  if (!iso) return "Never synced";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "Never synced";
  const diffMs = Date.now() - then;
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/** 'generator' == a pageGenerator (source/export); 'processor' == a
 * pageProcessor (destination/import) -- the CHECK constraint on
 * `celigo_flow_steps.role` (migration 094) allows only these two values. */
function stepKindLabel(role: string): "Source" | "Destination" {
  return role === "generator" ? "Source" : "Destination";
}

function isFlatScalarObject(value: unknown): value is Record<string, string | number | boolean | null> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((v) => v === null || typeof v !== "object")
  );
}

/** `mapping_json`/`responseMapping`'s CONFIRMED live shape (sanitizer.py):
 * `{fields: [{extract, generate}]}` -- `generate` is the destination
 * NetSuite field name, `extract` the source expression/value. */
function hasMappingFieldsShape(
  value: Record<string, unknown>,
): value is { fields: Array<{ extract?: unknown; generate?: unknown }> } {
  return Array.isArray(value.fields);
}

// ---------------------------------------------------------------------------
// Small shared bits
// ---------------------------------------------------------------------------

function KeyValueOrJson({ value }: { value: Record<string, unknown> }) {
  if (isFlatScalarObject(value)) {
    return (
      <Table>
        <TableBody>
          {Object.entries(value).map(([k, v]) => (
            <TableRow key={k}>
              <TableCell className="py-1.5 font-mono text-[12px] text-muted-foreground">{k}</TableCell>
              <TableCell className="py-1.5 font-mono text-[12px]">{v === null ? "—" : String(v)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    );
  }
  // Nested/array shape (e.g. filter's `expression.rules`) -- read the real
  // structure rather than mis-flattening it into rows that imply a meaning
  // it doesn't have.
  return (
    <pre className="max-h-48 overflow-auto rounded-lg border bg-muted/30 p-2 text-[11px] font-mono whitespace-pre-wrap break-words">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/** Fix round 1, Important: a query error must never render as (or alongside)
 * an empty state -- an operator reading "0 flows" or a permanent spinner has
 * no way to tell "genuinely nothing here" from "the request failed", and the
 * former actively misleads them into troubleshooting the wrong thing (e.g.
 * reconnecting a Celigo connection that's actually fine). Shared across all
 * four `useCeligo*` call-sites in this file so the distinction reads the
 * same everywhere. */
function ErrorNotice({
  message,
  onRetry,
  compact,
}: {
  message: string;
  onRetry?: () => void;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/5 text-destructive",
        compact ? "px-3 py-1.5 text-[12px]" : "px-3 py-2 text-[13px]",
      )}
    >
      <AlertTriangle className={cn("shrink-0", compact ? "h-3 w-3" : "h-3.5 w-3.5")} />
      <span className="flex-1">{message}</span>
      {onRetry && (
        <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

function FlowStatusPill({ flow }: { flow: CeligoFlowSummary }) {
  if (flow.disabled) {
    return (
      <Badge variant="outline" className="text-[11px] text-muted-foreground">
        Paused
      </Badge>
    );
  }
  if (flow.error_count > 0) {
    return (
      <Badge
        variant="outline"
        className="text-[11px] border-red-500/50 bg-red-500/10 text-red-700 dark:text-red-400"
      >
        {flow.signature_count} root cause{flow.signature_count === 1 ? "" : "s"}
        <span className="ml-1 opacity-70">
          ({flow.error_count} error{flow.error_count === 1 ? "" : "s"})
        </span>
      </Badge>
    );
  }
  return (
    <Badge
      variant="outline"
      className="text-[11px] border-green-500/50 bg-green-500/10 text-green-700 dark:text-green-400"
    >
      <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-green-600" aria-hidden />
      Healthy
    </Badge>
  );
}

function StepRow({ step, onOpenScript }: { step: CeligoFlowStep; onOpenScript: (scriptId: string) => void }) {
  const openableId = firstOpenableScriptId(step.attachments);
  return (
    <div className="flex items-center gap-2 py-1 text-[12px] text-muted-foreground">
      <span>
        {stepKindLabel(step.role)} ·{" "}
        <span className="font-mono">{step.adaptor_type || "Unknown adaptor"}</span>
      </span>
      {step.attachments.length > 0 &&
        (openableId ? (
          <button type="button" onClick={() => onOpenScript(openableId)} aria-label="Open attached script">
            <Badge
              variant="outline"
              className="text-[10px] border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400 hover:bg-amber-500/20 cursor-pointer"
            >
              {step.attachments.length} script{step.attachments.length === 1 ? "" : "s"}
            </Badge>
          </button>
        ) : (
          <Badge
            variant="outline"
            className="text-[10px] border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400"
          >
            {step.attachments.length} script{step.attachments.length === 1 ? "" : "s"}
          </Badge>
        ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// lvl2 — one flow row, its own caret expands lvl3 steps (fetched lazily via
// useCeligoFlowDetail, only while expanded -- not an eager N+1 fetch).
// ---------------------------------------------------------------------------

function FlowRow({
  flow,
  expanded,
  onToggle,
  onOpenDetail,
  onOpenScript,
}: {
  flow: CeligoFlowSummary;
  expanded: boolean;
  onToggle: () => void;
  onOpenDetail: () => void;
  onOpenScript: (scriptId: string) => void;
}) {
  const {
    data: detail,
    isLoading,
    isError,
    refetch,
  } = useCeligoFlowDetail(expanded ? flow.id : undefined);

  return (
    <div className={cn("border-t", flow.disabled && "opacity-60")}>
      <div className="flex items-center gap-2 px-4 py-2 pl-8">
        <button
          type="button"
          onClick={onToggle}
          aria-label={`${expanded ? "Collapse" : "Expand"} ${flow.name}`}
          className="text-muted-foreground hover:text-foreground transition-colors"
        >
          {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </button>
        <button
          type="button"
          onClick={onOpenDetail}
          className="text-[13px] font-medium text-left hover:underline"
        >
          {flow.name}
        </button>
        <span className="font-mono text-[11px] text-muted-foreground">{formatSchedule(flow.schedule)}</span>
        <span className="ml-auto">
          <FlowStatusPill flow={flow} />
        </span>
      </div>

      {expanded && (
        <div className="pb-2 pl-16">
          {isLoading && (
            <div className="flex items-center gap-1.5 py-1 text-[12px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              Loading steps…
            </div>
          )}
          {isError && (
            <ErrorNotice message="Couldn't load steps." onRetry={() => refetch()} compact />
          )}
          {!isLoading &&
            !isError &&
            detail?.steps.map((step) => <StepRow key={step.id} step={step} onOpenScript={onOpenScript} />)}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// lvl1 + integration head — one card per integration.
// ---------------------------------------------------------------------------

function IntegrationTree({
  integration,
  flows,
  flowsPending,
  flowsError,
  onRetryFlows,
  onSelectFlow,
  onOpenScript,
}: {
  integration: CeligoIntegration;
  flows: CeligoFlowSummary[];
  /** `status === "pending"` on this integration's flows query -- true while
   * fetching AND while a failed fetch sits paused for its retry. Rendered as
   * loading, never as "0 flows" (see `anyFlowsQueryUnresolved`). */
  flowsPending: boolean;
  flowsError: boolean;
  onRetryFlows: () => void;
  onSelectFlow: (flowId: string) => void;
  onOpenScript: (scriptId: string) => void;
}) {
  const [treeExpanded, setTreeExpanded] = useState(true);
  const [expandedFlowId, setExpandedFlowId] = useState<string | null>(null);
  const failingCount = flows.filter((f) => f.error_count > 0).length;

  return (
    <div className="rounded-xl border bg-card shadow-soft overflow-hidden">
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-[14px] font-semibold">{integration.name}</span>
          <Badge variant="outline" className="text-[11px] text-muted-foreground">
            {integration.sandbox ? "Sandbox" : "Production"}
          </Badge>
        </div>
      </div>

      {flowsError ? (
        // This integration's flows failed to load -- MUST NOT fall through
        // to the "0 flows" tree header below, which would read as a
        // healthy, empty integration rather than a failed request (fix
        // round 1, Important).
        <div className="px-4 py-3">
          <ErrorNotice message="Couldn't load this integration's flows." onRetry={onRetryFlows} compact />
        </div>
      ) : flowsPending ? (
        // Not resolved yet -- the same rule as the error branch: an
        // unresolved query must never render as a confident "0 flows".
        <div className="flex items-center gap-1.5 px-4 py-2 text-[12px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          Loading flows…
        </div>
      ) : (
        <>
          <button
            type="button"
            onClick={() => setTreeExpanded((v) => !v)}
            className="flex w-full items-center gap-2 bg-muted/40 px-4 py-2 text-left"
          >
            {treeExpanded ? (
              <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
            )}
            <span className="text-[13px] font-bold">
              {flows.length} flow{flows.length === 1 ? "" : "s"}
            </span>
            {failingCount > 0 && (
              <Badge
                variant="outline"
                className="text-[11px] border-red-500/50 bg-red-500/10 text-red-700 dark:text-red-400"
              >
                {failingCount} failing
              </Badge>
            )}
          </button>

          {treeExpanded &&
            flows.map((flow) => (
              <FlowRow
                key={flow.id}
                flow={flow}
                expanded={expandedFlowId === flow.id}
                onToggle={() => setExpandedFlowId((id) => (id === flow.id ? null : flow.id))}
                onOpenDetail={() => onSelectFlow(flow.id)}
                onOpenScript={onOpenScript}
              />
            ))}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Screen 03 — flow detail dialog.
// ---------------------------------------------------------------------------

function GraphNode({
  step,
  kind,
  highlight,
  onOpenScript,
}: {
  step: CeligoFlowStep;
  kind: "SOURCE" | "DESTINATION";
  highlight?: boolean;
  onOpenScript: (scriptId: string) => void;
}) {
  const openableId = firstOpenableScriptId(step.attachments);
  return (
    <div className={cn("min-w-[180px] rounded-lg border px-3 py-2", highlight && "border-green-500/50 bg-green-500/5")}>
      <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{kind}</p>
      <p className="mt-0.5 font-mono text-[12px]">{step.adaptor_type || "Unknown adaptor"}</p>
      {step.attachments.length > 0 &&
        (openableId ? (
          <button
            type="button"
            className="mt-1"
            onClick={() => onOpenScript(openableId)}
            aria-label="Open attached script"
          >
            <Badge
              variant="outline"
              className="text-[10px] font-mono border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400 hover:bg-amber-500/20 cursor-pointer"
            >
              {step.attachments.length} script{step.attachments.length === 1 ? "" : "s"}
            </Badge>
          </button>
        ) : (
          <Badge
            variant="outline"
            className="mt-1 text-[10px] font-mono border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400"
          >
            {step.attachments.length} script{step.attachments.length === 1 ? "" : "s"}
          </Badge>
        ))}
    </div>
  );
}

function FilterPanel({ step }: { step: CeligoFlowStep }) {
  if (!step.filter_json) return null;
  return (
    <div className="rounded-lg border p-3">
      <p className="text-[13px] font-medium">Filter</p>
      <p className="mt-0.5 text-[12px] text-muted-foreground">
        Determines which records this step processes — the reason a record can go through unmatched.
      </p>
      <div className="mt-2">
        <KeyValueOrJson value={step.filter_json} />
      </div>
    </div>
  );
}

function FieldMappingPanel({ step }: { step: CeligoFlowStep }) {
  if (!step.mapping_json) return null;
  const mapping = step.mapping_json;
  return (
    <div className="rounded-lg border p-3">
      <p className="text-[13px] font-medium">Field mapping</p>
      <p className="mt-0.5 text-[12px] text-muted-foreground">NetSuite field and the value written to it.</p>
      <div className="mt-2">
        {hasMappingFieldsShape(mapping) ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="h-8 text-[11px]">NetSuite field</TableHead>
                <TableHead className="h-8 text-[11px]">Value</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {mapping.fields.map((f, i) => (
                <TableRow key={i}>
                  <TableCell className="py-1.5 font-mono text-[12px] text-muted-foreground">
                    {f.generate == null ? "—" : String(f.generate)}
                  </TableCell>
                  <TableCell className="py-1.5 font-mono text-[12px]">
                    {f.extract == null ? "—" : String(f.extract)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <KeyValueOrJson value={mapping} />
        )}
      </div>
    </div>
  );
}

function FlowDetailDialog({
  flowId,
  onOpenChange,
  onOpenScript,
}: {
  flowId: string | null;
  onOpenChange: (open: boolean) => void;
  onOpenScript: (scriptId: string) => void;
}) {
  const {
    data: flow,
    isLoading,
    isError,
    refetch,
  } = useCeligoFlowDetail(flowId ?? undefined);
  const sources = flow?.steps.filter((s) => s.role === "generator") ?? [];
  const destinations = flow?.steps.filter((s) => s.role === "processor") ?? [];
  const stepsWithFilter = flow?.steps.filter((s) => s.filter_json) ?? [];
  const stepsWithMapping = flow?.steps.filter((s) => s.mapping_json) ?? [];

  return (
    <Dialog open={!!flowId} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        {isError ? (
          // MUST be checked before the loading/!flow branch below -- once a
          // query errors, isLoading is false and `flow` stays undefined
          // forever, so that branch alone gets stuck on "Loading flow…"
          // with no escape but closing the dialog (fix round 1, Important).
          <div className="flex flex-col items-center gap-3 py-8">
            <ErrorNotice message="Couldn't load this flow." onRetry={() => refetch()} />
          </div>
        ) : isLoading || !flow ? (
          <div className="flex items-center justify-center gap-2 py-8 text-[13px] text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading flow…
          </div>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>{flow.name}</DialogTitle>
              <DialogDescription className="font-mono text-[12px]">
                {formatSchedule(flow.schedule)}
              </DialogDescription>
            </DialogHeader>

            {flow.steps.length === 0 ? (
              <p className="py-2 text-[12px] text-muted-foreground">No steps configured on this flow.</p>
            ) : (
              <div className="flex items-start gap-3 overflow-x-auto py-2">
                {sources.map((s) => (
                  <GraphNode key={s.id} step={s} kind="SOURCE" highlight onOpenScript={onOpenScript} />
                ))}
                {sources.length > 0 && destinations.length > 0 && (
                  <span className="mt-4 text-muted-foreground" aria-hidden>
                    →
                  </span>
                )}
                <div className="flex flex-col gap-2">
                  {destinations.map((s) => (
                    <GraphNode key={s.id} step={s} kind="DESTINATION" onOpenScript={onOpenScript} />
                  ))}
                </div>
              </div>
            )}

            <div className="grid gap-4 pt-2 sm:grid-cols-2">
              <div className="space-y-4">
                {stepsWithFilter.length > 0 ? (
                  stepsWithFilter.map((s) => <FilterPanel key={s.id} step={s} />)
                ) : (
                  <p className="text-[12px] text-muted-foreground">No filter configured on this flow.</p>
                )}
              </div>
              <div className="space-y-4">
                {stepsWithMapping.length > 0 ? (
                  stepsWithMapping.map((s) => <FieldMappingPanel key={s.id} step={s} />)
                ) : (
                  <p className="text-[12px] text-muted-foreground">No field mapping configured on this flow.</p>
                )}
              </div>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Stats strip
// ---------------------------------------------------------------------------

function StatCard({
  label,
  value,
  tone,
}: {
  label: string;
  /** A count (Integrations/Flows/Open errors) or a formatted string (Last
   * synced's relative time / "Never synced" / a loading placeholder). */
  value: number | string;
  tone?: "err" | "ok";
}) {
  const isErrTone = tone === "err" && typeof value === "number" && value > 0;
  return (
    <div
      className={cn(
        "rounded-lg border border-l-2 bg-card px-3 py-2",
        isErrTone ? "border-l-red-500" : tone === "ok" ? "border-l-green-500" : "border-l-primary/40",
      )}
    >
      <p className="text-[11px] text-muted-foreground">{label}</p>
      <p
        data-testid="celigo-stat-value"
        className={cn(
          "text-[20px] font-semibold",
          isErrTone && "text-red-600",
          tone === "ok" && "text-green-600",
        )}
      >
        {value}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top-level export
// ---------------------------------------------------------------------------

export function CeligoFlowMap() {
  const {
    data: integrations,
    isLoading,
    isError: integrationsError,
    refetch: refetchIntegrations,
  } = useCeligoIntegrations();
  const integrationIds = (integrations ?? []).map((i) => i.id);
  const flowQueries = useCeligoAllFlows(integrationIds);
  const syncStatus = useCeligoSyncStatus();
  const [selectedFlowId, setSelectedFlowId] = useState<string | null>(null);
  const [scriptViewerId, setScriptViewerId] = useState<string | null>(null);

  if (isLoading) {
    return (
      <div className="rounded-xl border bg-card p-6 shadow-soft animate-pulse">
        <div className="h-6 w-40 bg-muted rounded" />
        <div className="mt-3 h-4 w-64 bg-muted rounded" />
      </div>
    );
  }

  if (integrationsError) {
    // MUST be its own branch, never the "no integrations" copy below (fix
    // round 1, Important) -- that copy tells the operator to (re)connect
    // Celigo, which is actively misleading when the connection is fine and
    // this request just failed.
    return (
      <div className="space-y-2">
        <h3 className="text-lg font-semibold">Flow Map</h3>
        <ErrorNotice
          message="Couldn't load your Celigo integrations."
          onRetry={() => refetchIntegrations()}
        />
      </div>
    );
  }

  if (!integrations || integrations.length === 0) {
    return (
      <div className="space-y-1">
        <h3 className="text-lg font-semibold">Flow Map</h3>
        <p className="text-[13px] text-muted-foreground">
          No Celigo integrations synced yet. Connect Celigo above, or check back after the next sync.
        </p>
      </div>
    );
  }

  const flowsByIntegration = new Map<string, CeligoFlowSummary[]>(
    integrations.map((integration, index) => [integration.id, flowQueries[index]?.data ?? []]),
  );
  const allFlows = Array.from(flowsByIntegration.values()).flat();
  const totalOpenErrors = allFlows.reduce((sum, f) => sum + f.error_count, 0);
  // WHOLE-BRANCH REVIEW FINDING 12: `flowQueries[index]?.data ?? []` above
  // makes a failed (or still-loading) per-integration query contribute ZERO
  // to `allFlows`/`totalOpenErrors`, with nothing distinguishing that from a
  // genuinely healthy zero -- the same "don't let an unresolved query's
  // silence read as a confident answer" defect the "Last synced" stat right
  // below already guards against. Applying the identical pattern here: any
  // unresolved per-integration flows query marks BOTH stats "—" rather than
  // silently presenting a partial total as complete.
  //
  // LIVE DEFECT (staging, 2026-09-01): this used `isLoading || isError`.
  // TanStack's `isLoading` is pending AND fetching, so a query whose fetch
  // failed at the transport layer and sat PAUSED for its retry (`status:
  // "pending"`, `fetchStatus: "paused"`) was neither -- 26 of 36 integrations
  // were counted as resolved-and-empty, and the strip summed the other 10 as
  // if they were the whole account. The predicate is `status !== "success"`,
  // which is exactly `isPending || isError`; anything narrower is a stand-in
  // that drifts from it.
  const anyFlowsQueryUnresolved = flowQueries.some((q) => q.isPending || q.isError);

  // Loading/error render a neutral placeholder rather than a fabricated
  // "Never synced" -- that string is a real, meaningful claim (fix round
  // 1's Important lesson applies here too, at smaller scale: don't let an
  // unresolved query's silence read as a confident answer).
  const syncedValue =
    syncStatus.isLoading || syncStatus.isError ? "—" : formatRelativeTime(syncStatus.data?.last_synced_at ?? null);
  const syncedOk = !syncStatus.isLoading && !syncStatus.isError && !!syncStatus.data?.last_synced_at;

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold">Flow Map</h3>
        <p className="mt-0.5 text-[13px] text-muted-foreground">
          Synced integrations, flows, and steps from your Celigo account.
        </p>
      </div>

      <div data-testid="celigo-stats-strip" className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="Integrations" value={integrations.length} />
        <StatCard label="Flows" value={anyFlowsQueryUnresolved ? "—" : allFlows.length} />
        <StatCard label="Open errors" value={anyFlowsQueryUnresolved ? "—" : totalOpenErrors} tone="err" />
        <StatCard label="Last synced" value={syncedValue} tone={syncedOk ? "ok" : undefined} />
      </div>

      <div className="space-y-3">
        {integrations.map((integration, index) => {
          const flowsQuery = flowQueries[index];
          return (
            <IntegrationTree
              key={integration.id}
              integration={integration}
              flows={flowsByIntegration.get(integration.id) ?? []}
              flowsPending={!!flowsQuery?.isPending}
              flowsError={!!flowsQuery?.isError}
              onRetryFlows={() => flowsQuery?.refetch()}
              onSelectFlow={setSelectedFlowId}
              onOpenScript={setScriptViewerId}
            />
          );
        })}
      </div>

      <p className="flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <span className="h-1.5 w-1.5 rounded-full bg-green-600" aria-hidden />
          Running clean
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-1.5 w-1.5 rounded-full bg-red-600" aria-hidden />
          Open errors
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/50" aria-hidden />
          Paused (disabled in Celigo)
        </span>
      </p>

      <FlowDetailDialog
        flowId={selectedFlowId}
        onOpenChange={(open) => !open && setSelectedFlowId(null)}
        onOpenScript={setScriptViewerId}
      />
      <CeligoScriptViewerDialog
        scriptId={scriptViewerId}
        onOpenChange={(open) => !open && setScriptViewerId(null)}
      />
    </div>
  );
}
