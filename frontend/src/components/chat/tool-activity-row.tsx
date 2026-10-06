"use client";

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { StreamingToolCall, ToolCallStep } from "@/lib/types";
import { SuiteQLToolCard } from "@/components/chat/suiteql-tool-card";
import { ToolCallStepCard } from "@/components/chat/tool-call-step";
import { WorkspaceToolCard } from "@/components/chat/workspace-tool-card";

/**
 * One row for all of a turn's data-gathering steps. While the turn runs, each
 * new step replaces the row's text; when it finishes the row collapses to
 * "Queried <source> · N steps · time" and expands to a compact step list.
 */

export interface ActivityStep {
  tool: string;
  params: Record<string, unknown>;
  summary?: string | null;
  durationMs?: number | null;
  status: "running" | "complete" | "error";
  /** The persisted step, kept so a query step can open its own result. */
  source?: ToolCallStep;
  /** "answer" when a result card shows this step's result; "check" when it only totals one. */
  role?: "answer" | "check" | null;
}

/** Tools that are presentation, not work: they never appear as steps. */
const PRESENTATION_TOOLS = new Set(["present_result", "compare_results"]);

/** How the backend words a failure, for messages saved before it recorded error=true:
 *  "Error: …", "Query failed", "NetSuite request failed: …", "API error 403: …",
 *  "Permission denied: …", "connection_unavailable". A failure word inside a sentence
 *  ("3 docs about failed logins", "0 errors") is not one. */
const LEGACY_FAILURE =
  /^\s*(?:error|failed|tool error|exception)\b|^[^:\n]{0,60}?\b(?:failed|error \d{3}|denied|timed out)\s*(?::|$|\bin\b)|^[a-z]+(?:_[a-z]+)*_(?:failed|unavailable|denied)$/i;

function rawName(tool: string): string {
  return tool.replace(/^ext__[a-f0-9]+__/, "");
}

function isMetabaseStep(step: ActivityStep): boolean {
  if (!step.tool.startsWith("ext__")) return false;
  const name = rawName(step.tool);
  const query = step.params?.query;
  if (query && typeof query === "object" && JSON.stringify(query).includes("mbql")) return true;
  return ["search", "read_resource", "construct_query", "execute_query", "execute_question"].includes(name);
}

function sourceOf(step: ActivityStep): string {
  const name = rawName(step.tool).toLowerCase();
  if (isMetabaseStep(step)) return "Metabase";
  if (name.includes("bigquery")) return "BigQuery";
  if (name.includes("celigo")) return "Celigo";
  if (name.includes("suiteql") || name.startsWith("netsuite") || name.startsWith("ns_")) return "NetSuite";
  if (name === "rag_search" || name === "web_search") return "sources";
  return "";
}

function tableName(source: unknown): string {
  if (Array.isArray(source) && source.length) return String(source[source.length - 1]);
  return typeof source === "string" ? source : "";
}

function entityName(table: string): string {
  let name = table.replace(/^spree_/, "").replace(/_/g, " ");
  if (name.endsWith("ies")) name = `${name.slice(0, -3)}y`;
  else if (name.endsWith("s") && !name.endsWith("ss")) name = name.slice(0, -1);
  return name;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function mbqlLabel(query: unknown): string {
  // Tool input is an untrusted shape (shown before execution and kept for failed calls).
  const stages = isRecord(query) && Array.isArray(query.stages) ? query.stages : null;
  const stage = stages && isRecord(stages[stages.length - 1]) ? (stages[stages.length - 1] as Record<string, unknown>) : null;
  if (!stage) return "Query";
  const table = tableName(stage["source-table"]).replace(/^spree_/, "").replace(/_/g, " ");
  // Join aliases ("o", "c") name the joined table, so "c.name" reads "country name".
  const joined: Record<string, string> = {};
  for (const join of Array.isArray(stage.joins) ? stage.joins.filter(isRecord) : []) {
    const joinStages = Array.isArray(join.stages) ? join.stages.filter(isRecord) : [];
    const source = joinStages.map((s) => s["source-table"]).find(Boolean);
    if (typeof join.alias === "string" && source) joined[join.alias] = entityName(tableName(source));
  }
  const breakout = Array.isArray(stage.breakout) ? stage.breakout : [];
  const by = breakout
    .map((field) => {
      if (!Array.isArray(field)) return String(field ?? "");
      const options = (field[1] ?? {}) as Record<string, unknown>;
      const target = field[field.length - 1];
      const name = Array.isArray(target) ? String(target[target.length - 1]) : String(target ?? "");
      const alias = typeof options["join-alias"] === "string" ? joined[options["join-alias"] as string] : undefined;
      return alias && !name.toLowerCase().startsWith(alias) ? `${alias} ${name}` : name;
    })
    .filter(Boolean)
    .map((name) => name.replace(/_/g, " ").toLowerCase());
  const measured = Array.isArray(stage.aggregation) && stage.aggregation.length > 0;
  if (!measured) return table ? `Looked up ${table}` : "Query";
  return `${table ? `${table[0].toUpperCase()}${table.slice(1)}` : "Rows"}${by.length ? ` by ${by.join(", ")}` : " total"}`;
}

/** Plain-language label, kind and output for one step. */
export function describeStep(step: ActivityStep): { kind: string; label: string; output: string } {
  const name = rawName(step.tool);
  const lower = name.toLowerCase();
  const rows = rowsOutput(step.summary);
  if (isMetabaseStep(step)) {
    if (name === "search") {
      const terms = Array.isArray(step.params.term_queries) ? step.params.term_queries.map(String) : [];
      return { kind: "Search", label: terms.length ? `Searched for ${terms.slice(0, 3).join(", ")}` : "Searched tables", output: "" };
    }
    if (name === "read_resource") {
      const uris = Array.isArray(step.params.uris) ? step.params.uris.map(String) : [];
      if (uris.length === 1 && /databases$/.test(uris[0])) return { kind: "Read", label: "Listed databases", output: "" };
      const tables = uris.filter((u) => /\/table\//.test(u)).length;
      return {
        kind: "Read",
        label: tables ? `Read fields of ${tables} table${tables === 1 ? "" : "s"}` : `Read ${uris.length || 1} resource${uris.length === 1 ? "" : "s"}`,
        output: "",
      };
    }
    return { kind: "Query", label: mbqlLabel(step.params.query), output: rows };
  }
  if (lower.includes("suiteql")) return { kind: "Query", label: "SuiteQL query", output: rows };
  if (lower.startsWith("bigquery_sql")) return { kind: "Query", label: "BigQuery query", output: rows };
  if (lower.includes("metadata") || lower.includes("schema")) return { kind: "Read", label: "Looked up the schema", output: "" };
  if (lower === "rag_search") return { kind: "Search", label: "Searched the knowledge base", output: "" };
  if (lower === "web_search") return { kind: "Search", label: "Searched the web", output: "" };
  if (lower.startsWith("workspace_")) return { kind: "Read", label: name.replace(/^workspace_/, "Workspace ").replace(/_/g, " "), output: "" };
  return { kind: "Tool", label: name.replace(/^ns_/, "").replace(/_/g, " "), output: rows };
}

function rowsOutput(summary: string | null | undefined): string {
  if (!summary) return "";
  if (/no rows/i.test(summary)) return "0 rows";
  const match = summary.match(/(\d[\d,]*)\s+rows?/i);
  if (match) return `${match[1]} ${match[1] === "1" ? "row" : "rows"}`;
  return "";
}

export function formatElapsed(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "";
  if (ms < 9_950) return `${(ms / 1000).toFixed(1)} s`;
  const total = Math.round(ms / 1000);
  if (total < 60) return `${total} s`;
  return `${Math.floor(total / 60)} min ${total % 60} s`;
}

function summaryTitle(steps: ActivityStep[]): string {
  const sources = Array.from(new Set(steps.map(sourceOf).filter(Boolean)));
  if (sources.length === 1) return sources[0] === "sources" ? "Searched sources" : `Queried ${sources[0]}`;
  if (sources.length > 1) return `Queried ${sources.slice(0, -1).join(", ")} and ${sources[sources.length - 1]}`;
  return "Worked through the request";
}

function countLabel(steps: ActivityStep[]): string {
  const queries = steps.filter((s) => describeStep(s).kind === "Query").length;
  if (steps.length === 1 && queries === 1) return "1 query";
  return `${steps.length} step${steps.length === 1 ? "" : "s"}`;
}

export function activityStepsFromCalls(
  calls: ToolCallStep[] | null | undefined,
  cards: { result_ids: string[]; control_result_ids?: string[] }[] = [],
): ActivityStep[] {
  const shown = new Set(cards.flatMap((card) => card.result_ids));
  const checks = new Set(cards.flatMap((card) => card.control_result_ids ?? []));
  return (calls ?? [])
    .map((call): ActivityStep => ({
      role: call.result_id && shown.has(call.result_id) ? "answer" : call.result_id && checks.has(call.result_id) ? "check" : null,
      tool: call.tool,
      params: call.params ?? {},
      summary: call.result_summary,
      durationMs: call.duration_ms,
      // The backend records a failed call as error=true. Messages saved before that flag fall
      // back to the backend's own error wording.
      status: call.error === true || LEGACY_FAILURE.test(call.result_summary ?? "") ? "error" : "complete",
      source: call,
    }))
    // A card that was shown is not a step of its own; a card that failed is.
    .filter((step) => !PRESENTATION_TOOLS.has(step.tool) || step.status === "error");
}

export function activityStepsFromStream(tools: StreamingToolCall[]): ActivityStep[] {
  return tools
    .filter((tool) => !PRESENTATION_TOOLS.has(tool.tool_name) || tool.status === "error")
    .map((tool) => ({
      tool: tool.tool_name,
      params: tool.tool_input ?? {},
      summary: tool.result_summary,
      durationMs: tool.duration_ms,
      status: tool.status,
    }));
}

/** The query card renders params.query as text; an MBQL object query is dropped, not rendered. */
function tableSafeStep(step: ToolCallStep): ToolCallStep {
  const query = step.params?.query;
  if (query === undefined || typeof query === "string") return step;
  const { query: _omitted, ...params } = step.params;
  void _omitted;
  return { ...step, params };
}

function useElapsed(running: boolean): number {
  const [start] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);
  return now - start;
}

function Tick() {
  return (
    <svg className="shrink-0 text-teal-500 dark:text-teal-400" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4">
      <path d="M5 12.5l4.5 4.5L19 7.5" />
    </svg>
  );
}

function StepList({ steps, userQuestion }: { steps: ActivityStep[]; userQuestion?: string }) {
  const [open, setOpen] = useState<number | null>(null);
  return (
    <div data-testid="tool-activity-steps" className="overflow-hidden rounded-[10px] border border-border bg-card">
      {steps.map((step, index) => {
        const { kind, label, output } = describeStep(step);
        // Every finished step opens: a SQL table result in the query card, anything else
        // (Metabase MBQL, schema reads, workspace steps, failures) in its plain detail view.
        const canOpen = !!step.source;
        const inAnswer = step.role === "answer";
        return (
          <div key={index} className="border-b border-border/60 last:border-b-0">
            <button
              type="button"
              disabled={!canOpen}
              onClick={() => setOpen((current) => (current === index ? null : index))}
              className={cn(
                "grid min-h-[46px] w-full items-center px-3.5 py-1 text-left text-[13px]",
                canOpen && "hover:bg-muted/40",
                inAnswer && "bg-teal-500/[0.06]",
              )}
              style={{ gridTemplateColumns: "28px 64px 1fr auto 80px" }}
            >
              <span className="tabular-nums text-muted-foreground/70">{index + 1}</span>
              <span
                className={cn(
                  "text-[12px] font-semibold",
                  step.status === "error" ? "text-red-500" : inAnswer ? "text-teal-500 dark:text-teal-400" : "text-muted-foreground/80",
                )}
              >
                {kind}
              </span>
              <span className="truncate text-foreground/85">{label}</span>
              <span
                className={cn(
                  "px-4 tabular-nums",
                  inAnswer ? "text-teal-500 dark:text-teal-400" : output === "0 rows" ? "text-muted-foreground/60" : "text-muted-foreground",
                )}
              >
                {step.status === "error" ? "failed" : [output, step.role === "answer" ? "in answer" : step.role === "check" ? "check" : null].filter(Boolean).join(" · ")}
              </span>
              <span className="text-right tabular-nums text-muted-foreground/70">{formatElapsed(step.durationMs)}</span>
            </button>
            {open === index && step.source && (
              <div className="border-t border-border/60 p-2">
                {step.source.result_payload?.kind === "table" || step.source.tool === "netsuite_suiteql" ? (
                  <SuiteQLToolCard step={tableSafeStep(step.source)} userQuestion={userQuestion} />
                ) : step.source.tool.startsWith("workspace_") ? (
                  <WorkspaceToolCard step={step.source} />
                ) : (
                  <ToolCallStepCard step={step.source} />
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function ToolActivityRow({
  steps,
  running = false,
  elapsedMs,
  userQuestion,
}: {
  steps: ActivityStep[];
  running?: boolean;
  /** Wall-clock time of the finished turn; while running the row keeps its own clock. */
  elapsedMs?: number | null;
  userQuestion?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const liveElapsed = useElapsed(running);
  if (steps.length === 0) return null;

  if (running) {
    const current = steps[steps.length - 1];
    const source = sourceOf(current);
    const { label } = describeStep(current);
    return (
      <div
        data-testid="tool-activity-running"
        aria-live="polite"
        className="flex min-h-11 items-center gap-3 rounded-lg border border-border bg-card px-3.5 py-2.5 text-[14px]"
      >
        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-blue-400" />
        <span className="font-medium text-foreground">
          {source && source !== "sources" ? `Querying ${source}` : "Working"}
        </span>
        <span className="truncate text-foreground/80">{label}</span>
        <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">
          Step {steps.length} · {formatElapsed(liveElapsed)}
        </span>
      </div>
    );
  }

  const wall = elapsedMs != null && Number.isFinite(elapsedMs) && elapsedMs >= 0 ? elapsedMs : null;
  const elapsed = formatElapsed(wall ?? steps.reduce((sum, s) => sum + (s.durationMs ?? 0), 0));
  const failed = steps.filter((s) => s.status === "error").length;
  return (
    <div className="flex flex-col gap-2.5">
      <button
        type="button"
        data-testid="tool-activity-done"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
        className="flex min-h-9 items-center gap-2.5 self-start rounded-lg border border-border px-3 py-1.5 text-[13px] text-foreground/85 transition-colors hover:bg-muted/40"
      >
        {failed ? <span className="shrink-0 text-amber-500">⚠</span> : <Tick />}
        <span className="font-medium">{summaryTitle(steps)}</span>
        <span className="text-muted-foreground">
          {countLabel(steps)}
          {elapsed ? ` · ${elapsed}` : ""}
        </span>
        {failed > 0 && (
          <span className="text-amber-600 dark:text-amber-400">
            · {failed} failed
          </span>
        )}
        {expanded ? <ChevronDown className="h-3 w-3 text-muted-foreground" /> : <ChevronRight className="h-3 w-3 text-muted-foreground" />}
      </button>
      {expanded && <StepList steps={steps} userQuestion={userQuestion} />}
    </div>
  );
}
