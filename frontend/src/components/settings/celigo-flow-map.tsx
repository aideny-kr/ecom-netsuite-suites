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
 * 3. (forced by reality) The mockup's "Last synced" stat and "Sync now"
 *    button are DROPPED. Task 8 exposes no endpoint for either: the nightly
 *    sync's freshness cursor lives in `cursor_states` (object_type=
 *    "celigo_flow_map", see `app/workers/tasks/celigo_flow_map_sync.py`) but
 *    no GET here returns it, and no endpoint triggers a sync. Inventing a
 *    button with no working action would be worse than omitting it.
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
  type CeligoFlowStep,
  type CeligoFlowSummary,
  type CeligoIntegration,
} from "@/hooks/use-celigo-flows";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";

// ---------------------------------------------------------------------------
// Formatters -- each grounded in a confirmed real shape, not the mockup.
// ---------------------------------------------------------------------------

/** Only `{type:"everyN", unit, value}` is CONFIRMED live (Task 8's own
 * fixture, `backend/tests/api/test_celigo_flows_api.py`'s `_seed_world`).
 * Any other schedule shape Celigo can send is real but unverified here --
 * falls back to a generic label rather than inventing a display string
 * (e.g. the mockup's ":05, :35") for a shape nobody has confirmed. */
export function formatSchedule(schedule: Record<string, unknown> | null): string {
  if (!schedule || Object.keys(schedule).length === 0) return "on demand";
  if (schedule.type === "everyN" && typeof schedule.value === "number" && typeof schedule.unit === "string") {
    return `every ${schedule.value} ${schedule.unit}`;
  }
  return "custom schedule";
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

function StepRow({ step }: { step: CeligoFlowStep }) {
  return (
    <div className="flex items-center gap-2 py-1 text-[12px] text-muted-foreground">
      <span>
        {stepKindLabel(step.role)} ·{" "}
        <span className="font-mono">{step.adaptor_type ?? "Unknown adaptor"}</span>
      </span>
      {step.attachments.length > 0 && (
        <Badge
          variant="outline"
          className="text-[10px] border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400"
        >
          {step.attachments.length} script{step.attachments.length === 1 ? "" : "s"}
        </Badge>
      )}
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
}: {
  flow: CeligoFlowSummary;
  expanded: boolean;
  onToggle: () => void;
  onOpenDetail: () => void;
}) {
  const { data: detail, isLoading } = useCeligoFlowDetail(expanded ? flow.id : undefined);

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
          {detail?.steps.map((step) => (
            <StepRow key={step.id} step={step} />
          ))}
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
  onSelectFlow,
}: {
  integration: CeligoIntegration;
  flows: CeligoFlowSummary[];
  onSelectFlow: (flowId: string) => void;
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
          />
        ))}
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
}: {
  step: CeligoFlowStep;
  kind: "SOURCE" | "DESTINATION";
  highlight?: boolean;
}) {
  return (
    <div className={cn("min-w-[180px] rounded-lg border px-3 py-2", highlight && "border-green-500/50 bg-green-500/5")}>
      <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{kind}</p>
      <p className="mt-0.5 font-mono text-[12px]">{step.adaptor_type ?? "Unknown adaptor"}</p>
      {step.attachments.length > 0 && (
        <Badge
          variant="outline"
          className="mt-1 text-[10px] font-mono border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400"
        >
          {step.attachments.length} script{step.attachments.length === 1 ? "" : "s"}
        </Badge>
      )}
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

function FlowDetailDialog({ flowId, onOpenChange }: { flowId: string | null; onOpenChange: (open: boolean) => void }) {
  const { data: flow, isLoading } = useCeligoFlowDetail(flowId ?? undefined);
  const sources = flow?.steps.filter((s) => s.role === "generator") ?? [];
  const destinations = flow?.steps.filter((s) => s.role === "processor") ?? [];
  const stepsWithFilter = flow?.steps.filter((s) => s.filter_json) ?? [];
  const stepsWithMapping = flow?.steps.filter((s) => s.mapping_json) ?? [];

  return (
    <Dialog open={!!flowId} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        {isLoading || !flow ? (
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

            <div className="flex items-start gap-3 overflow-x-auto py-2">
              {sources.map((s) => (
                <GraphNode key={s.id} step={s} kind="SOURCE" highlight />
              ))}
              {sources.length > 0 && destinations.length > 0 && (
                <span className="mt-4 text-muted-foreground" aria-hidden>
                  →
                </span>
              )}
              <div className="flex flex-col gap-2">
                {destinations.map((s) => (
                  <GraphNode key={s.id} step={s} kind="DESTINATION" />
                ))}
              </div>
            </div>

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

function StatCard({ label, value, tone }: { label: string; value: number; tone?: "err" }) {
  return (
    <div
      className={cn(
        "rounded-lg border border-l-2 bg-card px-3 py-2",
        tone === "err" && value > 0 ? "border-l-red-500" : "border-l-primary/40",
      )}
    >
      <p className="text-[11px] text-muted-foreground">{label}</p>
      <p
        data-testid="celigo-stat-value"
        className={cn("text-[20px] font-semibold", tone === "err" && value > 0 && "text-red-600")}
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
  const { data: integrations, isLoading } = useCeligoIntegrations();
  const integrationIds = (integrations ?? []).map((i) => i.id);
  const flowQueries = useCeligoAllFlows(integrationIds);
  const [selectedFlowId, setSelectedFlowId] = useState<string | null>(null);

  if (isLoading) {
    return (
      <div className="rounded-xl border bg-card p-6 shadow-soft animate-pulse">
        <div className="h-6 w-40 bg-muted rounded" />
        <div className="mt-3 h-4 w-64 bg-muted rounded" />
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

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-lg font-semibold">Flow Map</h3>
        <p className="mt-0.5 text-[13px] text-muted-foreground">
          Synced integrations, flows, and steps from your Celigo account.
        </p>
      </div>

      <div data-testid="celigo-stats-strip" className="grid grid-cols-3 gap-3">
        <StatCard label="Integrations" value={integrations.length} />
        <StatCard label="Flows" value={allFlows.length} />
        <StatCard label="Open errors" value={totalOpenErrors} tone="err" />
      </div>

      <div className="space-y-3">
        {integrations.map((integration) => (
          <IntegrationTree
            key={integration.id}
            integration={integration}
            flows={flowsByIntegration.get(integration.id) ?? []}
            onSelectFlow={setSelectedFlowId}
          />
        ))}
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

      <FlowDetailDialog flowId={selectedFlowId} onOpenChange={(open) => !open && setSelectedFlowId(null)} />
    </div>
  );
}
