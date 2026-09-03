"use client";

/**
 * Task 12 — the integration page (mockup screen 2): header, tabs, the
 * grouped flows table (topology glyph, schedule + stall pills), and the
 * per-step errors drawer. Replaces the Task 9 stub of the same name in
 * `celigo-surface.tsx`.
 *
 * Read-only surface: nothing here runs, enables, retries, resolves, syncs or
 * edits anything. There is no per-row Off/On toggle, no ▷ Run, no Actions
 * menu — Celigo's own UI carries those; this one only reads.
 *
 * Every query gates through `queryState()` (`lib/query-state.ts`), same as
 * every other Celigo page — a pending query is never rendered as empty, and
 * an errored one is never rendered as loading or as "0 flows".
 */

import { useMemo, useState } from "react";
import {
  useCeligoIntegrations,
  useCeligoIntegrationFlows,
  useCeligoSyncStatus,
  useCeligoIntegrationChanges,
  useCeligoFlowErrors,
  type CeligoIntegration,
  type CeligoFlowSummary,
  type CeligoRecordWrite,
  type CeligoConfigChange,
  type CeligoJson,
} from "@/hooks/use-celigo-flows";
import { queryState, type QueryState } from "@/lib/query-state";
import { cn } from "@/lib/utils";
import { parseSchedule, stallState, type ParsedSchedule } from "./schedule";
import { ErrorNotice, Medallions, Pill, SchedulePill, formatRelativeTime } from "./shared";
import { useCeligoRoute, type CeligoTab } from "./celigo-route";
import { CeligoBreadcrumb } from "./celigo-surface";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";

const NO_FLOWS: CeligoFlowSummary[] = [];
const NO_CHANGES: CeligoConfigChange[] = [];

// ---------------------------------------------------------------------------
// Pure helpers — exported for tests, so "how a flow groups" and "what the
// glyph shows" are each computed exactly once and checked without mounting
// the table.
// ---------------------------------------------------------------------------

/** The Steps glyph's two facts, straight off the flow summary's own
 * aggregates (Task 5) — no derivation needed, the glyph IS these two
 * numbers rendered as `◉→◇…◇→{steps}` (see `StepsGlyph` below). Kept as its
 * own function anyway (not inlined) because the render side needs a stable,
 * independently-testable seam: "what the glyph is told" vs. "how it draws
 * that" are two different bugs to catch. */
export function topologyGlyph(
  flow: Pick<CeligoFlowSummary, "router_count" | "step_count">,
): { routers: number; steps: number } {
  return { routers: flow.router_count, steps: flow.step_count };
}

type FlowGroupKey = "scheduled" | "on_demand" | "paused";

/** A flow's own On/Off state always wins over its schedule shape — a
 * disabled flow that still carries a cron string is "paused", not
 * "scheduled" (see the mockup's "kept: every 4 h" rows: the schedule is
 * shown, but the flow groups under Paused). Only a flow that is ON groups
 * by what its OWN schedule says: `on_demand` for `schedule: null` (or an
 * empty string), `scheduled` for anything else — including an `unknown`
 * shape, which still has *a* schedule, just not one this client can
 * humanise (see `parseSchedule`'s docstring). */
function flowGroupKey(flow: Pick<CeligoFlowSummary, "disabled" | "schedule">): FlowGroupKey {
  if (flow.disabled === true) return "paused";
  return parseSchedule(flow.schedule).kind === "on_demand" ? "on_demand" : "scheduled";
}

const GROUP_ORDER: readonly FlowGroupKey[] = ["scheduled", "on_demand", "paused"];
const GROUP_BASE_LABEL: Record<FlowGroupKey, string> = {
  scheduled: "On · scheduled",
  on_demand: "On · on demand",
  paused: "Paused in Celigo",
};

/** Buckets flows into the table's three group rows, in the mockup's own
 * fixed order (scheduled, then on demand, then paused) — never input order,
 * so a re-fetch that returns the same flows in a different order never
 * reshuffles the groups themselves. A group with zero flows is omitted
 * entirely rather than rendered empty (an integration with no paused flows
 * shows no "Paused in Celigo · 0" row at all). */
export function groupFlows(
  flows: CeligoFlowSummary[],
): { key: FlowGroupKey; label: string; flows: CeligoFlowSummary[] }[] {
  const buckets: Record<FlowGroupKey, CeligoFlowSummary[]> = { scheduled: [], on_demand: [], paused: [] };
  for (const flow of flows) buckets[flowGroupKey(flow)].push(flow);
  return GROUP_ORDER.filter((key) => buckets[key].length > 0).map((key) => ({
    key,
    label: `${GROUP_BASE_LABEL[key]} · ${buckets[key].length}`,
    flows: buckets[key],
  }));
}

/** "20 flows · 9 scheduled · 6 on demand · 5 paused · 94 steps · 11 routers
 * · 24 lookups · 30 scripts" — the header's own counts line. Mirrors
 * `celigo-integrations-page.tsx`'s `countsLine` zero-omission convention for
 * the schedule-mix segments (a fully-scheduled integration never shows "0
 * paused"), but always shows the four structural facts (flows, steps,
 * routers, lookups, scripts) regardless of count — they describe the
 * integration's shape, not an activity mix that can legitimately be zero
 * and uninteresting. Reproduced here rather than imported: that function is
 * private to its own file, and this header needs three more segments
 * `countsLine` doesn't carry. */
function headerCountsLine(integration: CeligoIntegration): string {
  const parts = [`${integration.flow_count} flow${integration.flow_count === 1 ? "" : "s"}`];
  if (integration.scheduled_count > 0) parts.push(`${integration.scheduled_count} scheduled`);
  if (integration.on_demand_count > 0) parts.push(`${integration.on_demand_count} on demand`);
  if (integration.paused_count > 0) parts.push(`${integration.paused_count} paused`);
  parts.push(`${integration.step_count} step${integration.step_count === 1 ? "" : "s"}`);
  parts.push(`${integration.router_count} router${integration.router_count === 1 ? "" : "s"}`);
  parts.push(`${integration.lookup_count} lookup${integration.lookup_count === 1 ? "" : "s"}`);
  parts.push(`${integration.script_count} script${integration.script_count === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

/** "writes salesorder ×19 · customer ×9 · itemfulfillment ×2 ·
 * customerdeposit ×2 · +2 custom records" — same top-4-then-collapse
 * convention as `celigo-integrations-page.tsx`'s `WritesLine` (see that
 * function's docstring for why the collapse only ever claims "custom
 * records" when every collapsed type actually is one), reproduced here as
 * plain text rather than JSX since the header renders it inline with the
 * counts line, not as its own block. */
function headerWritesLine(writes: CeligoRecordWrite[]): string {
  if (writes.length === 0) return "no NetSuite writes";
  const visible = writes.slice(0, 4);
  const overflow = writes.slice(4);
  const allCustom = overflow.length > 0 && overflow.every((w) => w.record_type.startsWith("customrecord_"));
  const parts = visible.map((w) => `${w.record_type} ×${w.count}`);
  if (overflow.length > 0) {
    parts.push(`+${overflow.length} ${allCustom ? `custom record${overflow.length === 1 ? "" : "s"}` : "more"}`);
  }
  return `writes ${parts.join(" · ")}`;
}

/** `old_value`/`new_value` are opaque JSON (`CeligoJson`) — a config change
 * can touch a string field, a boolean, or a whole object. Rendered as
 * literal text: primitives as themselves, `null` as "—" (a value that was
 * genuinely absent, not the string "null"), anything else stringified so a
 * change row never throws on a shape it didn't anticipate. */
function jsonDisplay(value: CeligoJson): string {
  if (value === null) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

// ---------------------------------------------------------------------------
// Small presentational pieces
// ---------------------------------------------------------------------------

function HeaderWrites({ writes }: { writes: CeligoRecordWrite[] }): JSX.Element {
  if (writes.length === 0) {
    return <span className="text-muted-foreground">no NetSuite writes</span>;
  }
  return <span className="font-mono">{headerWritesLine(writes)}</span>;
}

/** The Schedule column's cell — on-demand and unknown shapes are each their
 * own sentence (never Celigo's cron string dressed up as something it
 * isn't); a parsed cron shows the humanised label plus the verbatim elided
 * display string, "kept: " prefixed when the flow is paused (the schedule
 * itself didn't change, only whether it still runs). */
function ScheduleCell({
  disabled,
  parsed,
}: {
  disabled: boolean | null;
  parsed: ParsedSchedule;
}): JSX.Element {
  if (parsed.kind === "on_demand") {
    return <span className="text-muted-foreground">on demand</span>;
  }
  if (parsed.kind === "unknown") {
    return <span className="font-mono text-muted-foreground">{parsed.raw}</span>;
  }
  const label = disabled === true ? `kept: ${parsed.label}` : parsed.label;
  return (
    <span className="whitespace-nowrap">
      <span className="font-mono">{label}</span>{" "}
      <span className="font-mono text-[10.5px] text-muted-foreground">{parsed.display}</span>
    </span>
  );
}

/** "◉→◇◇→10" (2 routers) / "◉→3" (no routers) — one plain-text glyph node
 * rather than several styled spans, so the whole shape reads as a single
 * string at a glance (and is trivially matchable by exact text in tests). */
function StepsGlyph({ flow }: { flow: Pick<CeligoFlowSummary, "router_count" | "step_count"> }): JSX.Element {
  const { routers, steps } = topologyGlyph(flow);
  const text = routers > 0 ? `◉→${"◇".repeat(routers)}→${steps}` : `◉→${steps}`;
  return <span className="font-mono text-[11px] text-muted-foreground">{text}</span>;
}

/** Scripts column — a bare "0" for no scripts; otherwise the count, plus a
 * "N diverged" pill only when at least one attached script family actually
 * diverged (see `shared.tsx`'s docstring on why `content_diverged` is a
 * real possibility, not a sync gap, for the family this counts). */
function ScriptsCell({ flow }: { flow: Pick<CeligoFlowSummary, "script_count" | "diverged_family_count"> }): JSX.Element {
  if (flow.script_count === 0) return <span className="tabular-nums">0</span>;
  if (flow.diverged_family_count > 0) {
    return (
      <span
        className="tabular-nums text-amber-700 dark:text-amber-400"
        title="one or more attached script families have diverged copies"
      >
        {`${flow.script_count} · ${flow.diverged_family_count} diverged`}
      </span>
    );
  }
  return <span className="tabular-nums">{flow.script_count}</span>;
}

/** Errors column — "0" (a green pill when the flow is live, a bare dash
 * when it's paused: nothing is being checked for a flow Celigo isn't
 * running) or a clickable "{signature_count} root cause{s} ·
 * {error_count}" that opens the per-step drawer. `signature_count` leads —
 * the actionable number — with the raw count trailing it, same "root cause
 * leads" doctrine as the flow page's own error state (mockup screen 5). */
function ErrorsCell({
  flow,
  paused,
  onOpen,
}: {
  flow: Pick<CeligoFlowSummary, "error_count" | "signature_count">;
  paused: boolean;
  onOpen: () => void;
}): JSX.Element {
  if (flow.error_count === 0) {
    if (paused) return <span className="text-muted-foreground">—</span>;
    return (
      <Pill tone="ok" dot="solid">
        0
      </Pill>
    );
  }
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
      className="inline-flex"
    >
      <Pill tone="crit" dot="solid">
        {flow.signature_count} root cause{flow.signature_count === 1 ? "" : "s"} · {flow.error_count}
      </Pill>
    </button>
  );
}

function FlowRow({
  flow,
  lastSyncedAt,
  onOpenErrors,
  onOpenFlow,
}: {
  flow: CeligoFlowSummary;
  lastSyncedAt: string | null;
  onOpenErrors: (flowId: string, flowName: string) => void;
  onOpenFlow: (flowId: string) => void;
}): JSX.Element {
  const parsed = parseSchedule(flow.schedule);
  const stall = stallState({
    schedule: flow.schedule,
    disabled: flow.disabled,
    lastExecutedAt: flow.last_executed_at,
    lastSyncedAt,
  });
  const paused = flow.disabled === true;
  const syncClock = lastSyncedAt ? new Date(lastSyncedAt) : undefined;

  return (
    <TableRow
      data-paused={paused ? "true" : undefined}
      className={cn("cursor-pointer", paused && "opacity-70")}
      onClick={() => onOpenFlow(flow.id)}
    >
      <TableCell className="whitespace-normal font-medium">{flow.name}</TableCell>
      <TableCell>
        <StepsGlyph flow={flow} />
      </TableCell>
      <TableCell className="font-mono text-[12px] text-muted-foreground">
        {flow.writes.length > 0 ? flow.writes.map((w) => w.record_type).join(" · ") : "—"}
      </TableCell>
      <TableCell>
        <ScheduleCell disabled={flow.disabled} parsed={parsed} />
      </TableCell>
      <TableCell className="tabular-nums">
        <div className="flex flex-wrap items-center gap-1.5">
          <span>{formatRelativeTime(flow.last_executed_at, syncClock)}</span>
          <SchedulePill stall={stall} parsed={parsed} />
        </div>
      </TableCell>
      <TableCell className="tabular-nums text-muted-foreground">
        {formatRelativeTime(flow.celigo_last_modified)}
      </TableCell>
      <TableCell>
        <ErrorsCell flow={flow} paused={paused} onOpen={() => onOpenErrors(flow.id, flow.name)} />
      </TableCell>
      <TableCell>
        <ScriptsCell flow={flow} />
      </TableCell>
      <TableCell>
        {paused ? (
          <Pill tone="mute" dot="hollow">
            Paused
          </Pill>
        ) : (
          <Pill tone="ok" dot="solid">
            On
          </Pill>
        )}
      </TableCell>
    </TableRow>
  );
}

function FlowsTableSkeleton(): JSX.Element {
  return (
    <>
      <span className="sr-only">Loading flows…</span>
      <div aria-hidden="true" className="flex flex-col gap-1.5">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="h-7 animate-pulse rounded bg-muted" />
        ))}
      </div>
    </>
  );
}

function FlowsTable({
  flows,
  lastSyncedAt,
  onOpenErrors,
  onOpenFlow,
}: {
  flows: CeligoFlowSummary[];
  lastSyncedAt: string | null;
  onOpenErrors: (flowId: string, flowName: string) => void;
  onOpenFlow: (flowId: string) => void;
}): JSX.Element {
  const groups = groupFlows(flows);
  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="text-[10.5px]">Flow</TableHead>
            <TableHead className="text-[10.5px]">Steps</TableHead>
            <TableHead className="text-[10.5px]">Writes</TableHead>
            <TableHead className="text-[10.5px]">Schedule</TableHead>
            <TableHead className="text-[10.5px]">Last run</TableHead>
            <TableHead className="text-[10.5px]">Last updated</TableHead>
            <TableHead className="text-[10.5px]">Errors</TableHead>
            <TableHead className="text-[10.5px]">Scripts</TableHead>
            <TableHead className="text-[10.5px]">State</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {groups.flatMap((group) => [
            <TableRow key={`group-${group.key}`} className="hover:bg-transparent">
              <TableCell
                colSpan={9}
                className="bg-muted py-1 text-[10.5px] font-medium uppercase tracking-wide text-muted-foreground"
              >
                {group.label}
              </TableCell>
            </TableRow>,
            ...group.flows.map((flow) => (
              <FlowRow
                key={flow.id}
                flow={flow}
                lastSyncedAt={lastSyncedAt}
                onOpenErrors={onOpenErrors}
                onOpenFlow={onOpenFlow}
              />
            )),
          ])}
        </TableBody>
      </Table>
    </div>
  );
}

function ScriptsTab({ flows }: { flows: CeligoFlowSummary[] }): JSX.Element {
  const scriptsTotal = flows.reduce((sum, f) => sum + f.script_count, 0);
  const withScripts = flows.filter((f) => f.script_count > 0);
  return (
    <div className="flex flex-col gap-2 text-[13px]">
      <p className="text-muted-foreground">
        {scriptsTotal} script{scriptsTotal === 1 ? "" : "s"} across {flows.length} flow
        {flows.length === 1 ? "" : "s"} · the Scripts view ships separately
      </p>
      {withScripts.length > 0 && (
        <ul className="flex flex-col gap-1">
          {withScripts.map((f) => (
            <li key={f.id} className="flex items-center justify-between rounded-lg border px-2.5 py-1.5">
              <span>{f.name}</span>
              <ScriptsCell flow={f} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ErrorsTab({ flows, lastSyncedAt }: { flows: CeligoFlowSummary[]; lastSyncedAt: string | null }): JSX.Element {
  if (flows.length === 0) {
    return (
      <p className="text-[13px] text-muted-foreground">
        No open errors. Celigo reported 0 on the last sync, {formatRelativeTime(lastSyncedAt)}.
      </p>
    );
  }
  return (
    <ul className="flex flex-col gap-1 text-[13px]">
      {flows.map((f) => (
        <li key={f.id} className="flex items-center justify-between rounded-lg border px-2.5 py-1.5">
          <span>{f.name}</span>
          <Pill tone="crit" dot="solid">
            {f.signature_count} root cause{f.signature_count === 1 ? "" : "s"} · {f.error_count}
          </Pill>
        </li>
      ))}
    </ul>
  );
}

function ChangesTab({
  changes,
  changesState,
  onRetry,
}: {
  changes: CeligoConfigChange[];
  changesState: QueryState;
  onRetry: () => void;
}): JSX.Element {
  if (changesState === "pending") {
    return <p className="text-[13px] text-muted-foreground">Loading changes…</p>;
  }
  if (changesState === "error") {
    return <ErrorNotice message="Couldn't load changes." onRetry={onRetry} />;
  }
  if (changes.length === 0) {
    return (
      <p className="text-[13px] text-muted-foreground">
        No configuration changes recorded since syncing began.
      </p>
    );
  }
  return (
    <ul className="flex flex-col gap-1 text-[13px]">
      {changes.map((c) => (
        <li key={c.id} className="rounded-lg border px-2.5 py-1.5 font-mono text-[12px]">
          {c.field} · {jsonDisplay(c.old_value)} → {jsonDisplay(c.new_value)} · {formatRelativeTime(c.created_at)}
        </li>
      ))}
    </ul>
  );
}

/** The per-step errors drawer (mockup note: 'Clicking an Errors count opens
 * Celigo's per-step "Flow: name" drawer'). Positioned as a right-hand panel
 * rather than the shared `DialogContent`'s centered default — every class
 * in the default that conflicts with that (position, translate, width) is
 * overridden below; `cn`'s `twMerge` resolves each pair to the last one
 * given, so this list must name every conflicting group, not just add new
 * ones. */
function CeligoFlowErrorsDrawer({
  flowId,
  flowName,
  onOpenChange,
}: {
  flowId: string | null;
  flowName: string;
  onOpenChange: (open: boolean) => void;
}): JSX.Element {
  const errorsQuery = useCeligoFlowErrors(flowId ?? undefined);
  const state = queryState(errorsQuery);

  return (
    <Dialog open={!!flowId} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn(
          "inset-y-0 right-0 left-auto top-0 w-[520px] max-w-none translate-x-0 translate-y-0",
          "flex flex-col gap-3 overflow-y-auto rounded-none border-l",
        )}
      >
        <DialogHeader>
          <DialogTitle>Flow: {flowName}</DialogTitle>
          <DialogDescription className="sr-only">Per-step open errors for this flow</DialogDescription>
        </DialogHeader>
        {state === "pending" ? (
          <p className="text-[13px] text-muted-foreground">Loading errors…</p>
        ) : state === "error" ? (
          <ErrorNotice message="Couldn't load errors for this flow." onRetry={() => errorsQuery.refetch()} />
        ) : errorsQuery.data!.groups.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No open errors on this flow.</p>
        ) : (
          <ul className="flex flex-col gap-2 text-[12px]">
            {errorsQuery.data!.groups.map((group, i) => (
              <li key={i} className="rounded-lg border p-2.5">
                <div className="font-mono text-[11px] text-muted-foreground">
                  {group.step_ids.filter((s): s is string => !!s).join(", ") || "—"}
                </div>
                <div className="tabular-nums">
                  {group.count} error{group.count === 1 ? "" : "s"}
                </div>
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}

function PageSkeleton(): JSX.Element {
  return (
    <>
      <span className="sr-only">Loading integration…</span>
      <div aria-hidden="true" className="flex flex-col gap-3">
        <div className="h-6 w-64 animate-pulse rounded bg-muted" />
        <div className="h-4 w-96 animate-pulse rounded bg-muted" />
        <div className="h-64 animate-pulse rounded-xl border bg-card" />
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

export function CeligoIntegrationPage(): JSX.Element {
  const route = useCeligoRoute();
  const integrationId = route.integrationId;

  const integrationsQuery = useCeligoIntegrations();
  const flowsQuery = useCeligoIntegrationFlows(integrationId ?? undefined);
  const syncStatusQuery = useCeligoSyncStatus();
  const changesQuery = useCeligoIntegrationChanges(integrationId ?? undefined);
  const [errorsDrawer, setErrorsDrawer] = useState<{ flowId: string; flowName: string } | null>(null);

  const integrationsState = queryState(integrationsQuery);
  const flowsState = queryState(flowsQuery);
  const syncStatusState = queryState(syncStatusQuery);
  const changesState = queryState(changesQuery);

  const lastSyncedAt = syncStatusState === "success" ? syncStatusQuery.data?.last_synced_at ?? null : null;
  const integration =
    integrationsState === "success" ? integrationsQuery.data!.find((i) => i.id === integrationId) : undefined;
  const flows = flowsQuery.data ?? NO_FLOWS;
  const changes = changesQuery.data ?? NO_CHANGES;

  const scriptsTotal = useMemo(() => flows.reduce((sum, f) => sum + f.script_count, 0), [flows]);
  const errorFlows = useMemo(() => flows.filter((f) => f.error_count > 0), [flows]);

  let body: JSX.Element;
  if (integrationsState === "pending") {
    body = <PageSkeleton />;
  } else if (integrationsState === "error") {
    body = <ErrorNotice message="Couldn't load integrations." onRetry={() => integrationsQuery.refetch()} />;
  } else if (!integration) {
    body = (
      <div className="flex flex-col items-start gap-2 text-[13px] text-muted-foreground">
        <p>This integration is not in the last sync.</p>
        <button
          type="button"
          className="font-medium text-foreground underline"
          onClick={() => route.go.integrations()}
        >
          Back to My integrations
        </button>
      </div>
    );
  } else {
    const flowsLoading = flowsState === "pending" || syncStatusState === "pending";
    body = (
      <>
        <div className="flex flex-wrap items-start gap-3.5">
          <div>
            <h3 className="text-[22px] font-semibold tracking-tight">{integration.name}</h3>
            <div className="mt-1 flex flex-wrap items-center gap-2.5 text-[12px] tabular-nums text-muted-foreground">
              <Pill tone="mute">Production</Pill>
              <Medallions families={integration.adaptor_families} />
              <span>{headerCountsLine(integration)}</span>
              <HeaderWrites writes={integration.writes} />
            </div>
          </div>
        </div>

        <Tabs
          value={route.tab}
          onValueChange={(v) => route.go.integration(integration.id, v as CeligoTab)}
        >
          <TabsList>
            <TabsTrigger value="flows">Flows {flowsState === "success" ? flows.length : "…"}</TabsTrigger>
            <TabsTrigger value="scripts">Scripts {flowsState === "success" ? scriptsTotal : "…"}</TabsTrigger>
            <TabsTrigger value="errors">Errors {flowsState === "success" ? errorFlows.length : "…"}</TabsTrigger>
            <TabsTrigger value="changes">Changes {changesState === "success" ? changes.length : "…"}</TabsTrigger>
          </TabsList>
          <TabsContent value="flows">
            {flowsLoading ? (
              <FlowsTableSkeleton />
            ) : flowsState === "error" ? (
              <ErrorNotice message="Couldn't load flows." onRetry={() => flowsQuery.refetch()} />
            ) : (
              <FlowsTable
                flows={flows}
                lastSyncedAt={lastSyncedAt}
                onOpenErrors={(flowId2, flowName) => setErrorsDrawer({ flowId: flowId2, flowName })}
                onOpenFlow={route.go.flow}
              />
            )}
          </TabsContent>
          <TabsContent value="scripts">
            {flowsState === "pending" ? (
              <FlowsTableSkeleton />
            ) : flowsState === "error" ? (
              <ErrorNotice message="Couldn't load flows." onRetry={() => flowsQuery.refetch()} />
            ) : (
              <ScriptsTab flows={flows} />
            )}
          </TabsContent>
          <TabsContent value="errors">
            {flowsLoading ? (
              <FlowsTableSkeleton />
            ) : flowsState === "error" ? (
              <ErrorNotice message="Couldn't load flows." onRetry={() => flowsQuery.refetch()} />
            ) : (
              <ErrorsTab flows={errorFlows} lastSyncedAt={lastSyncedAt} />
            )}
          </TabsContent>
          <TabsContent value="changes">
            <ChangesTab changes={changes} changesState={changesState} onRetry={() => changesQuery.refetch()} />
          </TabsContent>
        </Tabs>
      </>
    );
  }

  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <CeligoBreadcrumb
        items={[
          { label: "My integrations", onClick: () => route.go.integrations() },
          { label: integration?.name ?? integrationId ?? "" },
        ]}
      />
      <div className="flex flex-1 min-h-0 flex-col gap-3 overflow-auto p-4">{body}</div>
      <CeligoFlowErrorsDrawer
        flowId={errorsDrawer?.flowId ?? null}
        flowName={errorsDrawer?.flowName ?? ""}
        onOpenChange={(open) => {
          if (!open) setErrorsDrawer(null);
        }}
      />
    </div>
  );
}
