"use client";

import type { ChatMessage, ChartData, ClarificationData, StreamingToolCall, WriteConfirmationData } from "@/lib/types";

export interface FinancialReportData {
  report_type: string;
  period: string;
  columns: string[];
  rows: Record<string, any>[];
  summary: Record<string, any>;
}

export interface DataTableData {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  query: string;
  truncated: boolean;
  /** True when this table came from metric_compute (suppress_llm_value=true in the SSE payload).
   *  When set, the component must hide SuiteQL-specific affordances (query expander,
   *  re-run/export-as-query, save-query) because `query` is a metric key, not SQL. */
  isMetric?: boolean;
  /** The honesty channel any data_table tool may carry (spec
   *  docs/superpowers/specs/2026-09-04-celigo-chat-access.md §8) — snapshot age,
   *  unchecked-error flags, stall verdicts, etc. Absent when the backend result
   *  carried none; the card renders nothing in that case. */
  caveats?: string[];
  /** Conversation-wide result id (rN); a result card names the ids it presents. */
  result_id?: string;
}

/**
 * Single source of truth for deciding whether a data_table is a metric table.
 *
 * Metric tables (from metric_compute) carry `suppress_llm_value: true` on the raw/persisted
 * SSE payload; the FE-only derived `isMetric` flag is set from it. This helper is idempotent
 * over BOTH shapes that reach it:
 *   (a) the raw persisted/SSE shape carrying snake_case `suppress_llm_value`, and
 *   (b) an already-normalized DataTableData that already has a boolean `isMetric`.
 *
 * It MUST be the one place that decides metric-ness so the live-stream path and the
 * hydration path cannot drift. Uses strict `=== true` (no truthy-string coercion) and
 * relies SOLELY on the authoritative flag(s) — never a columns/query heuristic, which
 * would risk locking down a legitimate SuiteQL query whose columns happen to be
 * Metric/Value/Unit/Period.
 */
export function deriveDataTableIsMetric(d: Record<string, unknown>): boolean {
  return d.isMetric === true || d.suppress_llm_value === true;
}

/**
 * Coerce a raw persisted/SSE data_table payload into a fully-typed DataTableData, deriving
 * `isMetric` via deriveDataTableIsMetric. Mirrors the field coercions in normalizeStreamEvent's
 * data_table branch. Idempotent: feeding an already-normalized object back through it preserves
 * `isMetric` (recovered from `suppress_llm_value` if the camelCase flag was somehow dropped).
 */
export function coerceDataTableData(d: Record<string, unknown>): DataTableData {
  return {
    columns: Array.isArray(d.columns) ? (d.columns as string[]) : [],
    rows: Array.isArray(d.rows) ? (d.rows as unknown[][]) : [],
    row_count: typeof d.row_count === "number" ? d.row_count : 0,
    query: typeof d.query === "string" ? d.query : "",
    truncated: Boolean(d.truncated),
    isMetric: deriveDataTableIsMetric(d),
    ...(Array.isArray(d.caveats) && d.caveats.length > 0 ? { caveats: d.caveats as string[] } : {}),
    ...(typeof d.result_id === "string" ? { result_id: d.result_id } : {}),
  };
}

/** True when a result card presents this table (by result id), so the raw table is redundant. */
export function tableCoveredByCards(table: { result_id?: string } | null | undefined, cards: ResultCardData[]): boolean {
  if (!table?.result_id || cards.length === 0) return false;
  if (Array.isArray((table as { caveats?: unknown }).caveats) && (table as { caveats: unknown[] }).caveats.length > 0) {
    return false;
  }
  return cards.some((card) => card.result_ids.includes(table.result_id!));
}

export interface TaskOutputData {
  sku_count: number;
  currency_count: number;
  output_files: Record<string, string>;
  preview: Record<string, any>[];
  template_mode: boolean;
}

export interface SheetsLinkData {
  url: string;
  spreadsheet_id: string;
  title: string;
  shared_with?: string | null;
}

export interface DocsLinkData {
  url: string;
  doc_id: string;
  title: string;
  shared_with?: string | null;
}

export interface ReportReadyData {
  report_id: string;
  title: string;
  url: string;
  section_count?: number;
}

/** Group preparation's own counts, streamed as each order finishes (server numbers, never the model's). */
export interface PreparationProgressData {
  checked: number;
  total: number;
  ready: number;
  set_aside: Array<{ label: string; count: number }>;
  now: string[];
}

/** One cause in a group breakdown. Every amount is the server's; the model never states them. */
export interface GroupBreakdownCause {
  cause: string;
  label: string;
  why: string;
  next_step: string;
  /** The server's short name for the next step; the card only colours it. */
  next_pill?: string;
  next_label: string;
  orders: number;
  order_references: string[];
  /** The exact case of each order, paired with order_references by position. */
  case_ids?: string[];
  /** null when an amount is missing for some order: unknown, never zero. */
  amounts: Record<string, string | null>;
  /** The amount this cause shows, chosen by the server; null when none is known and non-zero. */
  primary?: { metric: string; amount: string } | null;
  facts: Array<{ fact: string; orders: number; kind?: string }>;
}

/** A group (or one order) split into causes, from saved evidence and at most two NetSuite reads. */
export interface GroupBreakdownData {
  group_id: string | null;
  case_id: string | null;
  scope: { review_run_ids?: string[] | null; status?: string | null; search?: string | null } | null;
  pattern: string | null;
  currency: string | null;
  orders: number;
  totals: Record<string, string | null>;
  causes: GroupBreakdownCause[];
  checked: {
    saved_evidence: number;
    saved_source_orders: number;
    /** complete | unavailable (saved Solidus orders could not be read) | not_configured */
    saved_source?: string;
    netsuite: string;
    netsuite_orders: number;
    seconds: number;
  };
}

/** Shape check for live and saved breakdowns: a malformed payload renders nothing rather than a broken card. */
export function isGroupBreakdown(value: unknown): value is GroupBreakdownData {
  if (!value || typeof value !== "object") return false;
  const data = value as Record<string, unknown>;
  return (
    typeof data.orders === "number" &&
    !!data.totals &&
    typeof data.totals === "object" &&
    !!data.checked &&
    typeof data.checked === "object" &&
    Array.isArray(data.causes) &&
    data.causes.every(
      (cause) =>
        !!cause &&
        typeof cause === "object" &&
        typeof (cause as GroupBreakdownCause).label === "string" &&
        typeof (cause as GroupBreakdownCause).orders === "number" &&
        Array.isArray((cause as GroupBreakdownCause).order_references) &&
        Array.isArray((cause as GroupBreakdownCause).facts) &&
        !!(cause as GroupBreakdownCause).amounts &&
        typeof (cause as GroupBreakdownCause).amounts === "object",
    )
  );
}

/** One column of a server-built result card (present_result / compare_results). */
export interface ResultCardColumn {
  key: string;
  label: string;
  format: "text" | "integer" | "number" | "currency" | "percent" | "date" | "delta";
  currency?: string | null;
  align: "left" | "right";
  /** Comparison cards group a measure's columns under one header (e.g. "Units"). */
  group?: string | null;
}

/** A result card. Every figure on it is computed by the server, never by the model. */
export interface ResultCardData {
  card_id: string;
  kind: "table" | "comparison";
  result_ids: string[];
  /** Ungrouped results used only to total and check the card. */
  control_result_ids?: string[];
  title: string;
  source: string;
  subtitle?: string | null;
  as_of?: string | null;
  scope?: string | null;
  queries: { label: string; text: string }[];
  columns: ResultCardColumn[];
  rows: unknown[][];
  row_flags?: ("diff" | "missing" | null)[] | null;
  share?: { label: string; of: number; values: number[] } | null;
  totals?: (number | null)[] | null;
  totals_label?: string | null;
  check?: { status: "ok" | "warn"; text: string } | null;
  tiles: { label: string; value: number; format: string; currency?: string | null }[];
  top_n: number;
  more_label?: string | null;
  less_label?: string | null;
  collapsed: boolean;
  collapsed_note?: string | null;
  no_total_reason?: string | null;
  headline?: string | null;
  detail?: string | null;
  truncated?: boolean;
}

/** Validate a persisted or streamed card; malformed payloads render nothing. */
export function coerceResultCard(raw: unknown): ResultCardData | null {
  if (!raw || typeof raw !== "object") return null;
  const d = raw as Record<string, unknown>;
  if (typeof d.card_id !== "string" || typeof d.title !== "string") return null;
  if (!Array.isArray(d.columns) || !Array.isArray(d.rows)) return null;
  const formats = ["text", "integer", "number", "currency", "percent", "date", "delta"];
  const columns = (d.columns as unknown[]).map((raw, index): ResultCardColumn => {
    const c = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
    return {
      key: typeof c.key === "string" ? c.key : `c${index}`,
      label: typeof c.label === "string" ? c.label : String(c.key ?? ""),
      format: (formats.includes(c.format as string) ? c.format : "text") as ResultCardColumn["format"],
      currency: typeof c.currency === "string" ? c.currency : null,
      align: c.align === "right" ? "right" : "left",
      group: typeof c.group === "string" ? c.group : null,
    };
  });
  const width = columns.length;
  const kept = (d.rows as unknown[]).map((row, index) => [row, index] as const).filter(([row]) => Array.isArray(row));
  const fit = (row: unknown[]) => (row.length >= width ? row : [...row, ...Array(width - row.length).fill(null)]);
  // Every field is rebuilt from a checked value: nothing from the payload reaches React unchecked.
  const text = (value: unknown) => (typeof value === "string" ? value : null);
  const strings = (value: unknown) => (Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : []);
  const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
  const rawShare = d.share && typeof d.share === "object" ? (d.share as Record<string, unknown>) : null;
  const shareValues = rawShare && Array.isArray(rawShare.values) ? (rawShare.values as unknown[]) : null;
  const shareValid =
    !!rawShare &&
    typeof rawShare.label === "string" &&
    finite(rawShare.of) &&
    !!shareValues &&
    shareValues.length >= (d.rows as unknown[]).length &&
    shareValues.every(finite);
  const totals =
    Array.isArray(d.totals) && d.totals.length >= width && d.totals.every((v) => v === null || finite(v))
      ? (d.totals as (number | null)[])
      : null;
  const flags = Array.isArray(d.row_flags) ? (d.row_flags as unknown[]) : null;
  const flagOf = (value: unknown) => (value === "diff" || value === "missing" ? value : null);
  const rawCheck = d.check && typeof d.check === "object" ? (d.check as Record<string, unknown>) : null;
  return {
    card_id: d.card_id,
    kind: d.kind === "comparison" ? "comparison" : "table",
    result_ids: strings(d.result_ids),
    control_result_ids: strings(d.control_result_ids),
    title: d.title,
    source: text(d.source) ?? "",
    subtitle: text(d.subtitle),
    as_of: text(d.as_of),
    scope: text(d.scope),
    queries: Array.isArray(d.queries)
      ? (d.queries as unknown[]).filter(
          (q): q is { label: string; text: string } =>
            !!q && typeof (q as { label?: unknown }).label === "string" && typeof (q as { text?: unknown }).text === "string",
        ).map((q) => ({ label: q.label, text: q.text }))
      : [],
    columns,
    rows: kept.map(([row]) => fit(row as unknown[])),
    row_flags: flags ? kept.map(([, index]) => flagOf(flags[index])) : null,
    share: shareValid
      ? { label: rawShare!.label as string, of: rawShare!.of as number, values: kept.map(([, index]) => shareValues![index] as number) }
      : null,
    totals,
    totals_label: text(d.totals_label),
    check:
      rawCheck && (rawCheck.status === "ok" || rawCheck.status === "warn") && typeof rawCheck.text === "string"
        ? { status: rawCheck.status, text: rawCheck.text }
        : null,
    tiles: Array.isArray(d.tiles)
      ? (d.tiles as unknown[]).flatMap((raw) => {
          const t = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
          if (typeof t.label !== "string" || !finite(t.value)) return [];
          const format = formats.includes(t.format as string) ? (t.format as string) : "integer";
          return [{ label: t.label, value: t.value, format, currency: text(t.currency) }];
        })
      : [],
    top_n: finite(d.top_n) ? d.top_n : (d.rows as unknown[]).length,
    more_label: text(d.more_label),
    less_label: text(d.less_label),
    collapsed: d.collapsed === true,
    collapsed_note: text(d.collapsed_note),
    no_total_reason: text(d.no_total_reason),
    headline: text(d.headline),
    detail: text(d.detail),
    truncated: d.truncated === true,
  };
}

/** Cards persisted on a message's structured_output (``result_cards``). */
export function resultCardsOf(structuredOutput: unknown): ResultCardData[] {
  if (!structuredOutput || typeof structuredOutput !== "object") return [];
  const cards = (structuredOutput as Record<string, unknown>).result_cards;
  if (!Array.isArray(cards)) return [];
  return cards.map(coerceResultCard).filter((c): c is ResultCardData => c !== null);
}

export type StreamBlock =
  | { type: "text"; content: string; id: string }
  | { type: "result_card"; data: ResultCardData; id: string }
  | { type: "tool"; tool: StreamingToolCall; id: string }
  | { type: "data_table"; data: DataTableData; id: string }
  | { type: "chart"; data: ChartData; id: string }
  | { type: "financial_report"; data: FinancialReportData; id: string }
  | { type: "task_output"; data: TaskOutputData; id: string }
  | { type: "sheets_link"; data: SheetsLinkData; id: string }
  | { type: "docs_link"; data: DocsLinkData; id: string }
  | { type: "report_ready"; data: ReportReadyData; id: string }
  | { type: "thinking"; content: string; isActive: boolean; id: string }
  | { type: "write_confirmation"; data: WriteConfirmationData; id: string }
  | { type: "preparation_progress"; data: PreparationProgressData; id: string }
  | { type: "group_breakdown"; data: GroupBreakdownData; id: string };

export type ChatStreamEvent =
  | { type: "text"; content: string }
  | { type: "tool_status"; content: string }
  | { type: "confidence"; score: number; explanation: string }
  | { type: "importance"; tier: number; label: string; needs_review: boolean }
  | { type: "financial_report"; data: FinancialReportData }
  | { type: "data_table"; data: DataTableData }
  | { type: "task_output"; data: TaskOutputData }
  | { type: "sheets_link"; data: SheetsLinkData }
  | { type: "docs_link"; data: DocsLinkData }
  | { type: "report_ready"; data: ReportReadyData }
  | { type: "result_card"; data: ResultCardData }
  | { type: "drive_sources"; sources: Record<string, string> }
  | { type: "chart"; data: ChartData }
  | { type: "clarification_required"; data: ClarificationData }
  | { type: "preparation_progress"; data: PreparationProgressData }
  | { type: "group_breakdown"; data: GroupBreakdownData }
  | { type: "error"; error: string }
  | { type: "message"; message: ChatMessage }
  | { type: "tool_start"; tool_name: string; tool_input: Record<string, unknown>; step: number }
  | { type: "tool_end"; tool_name: string; step: number; duration_ms: number; success: boolean; result_summary: string };

interface ParsedSseBuffer {
  events: ChatStreamEvent[];
  remainder: string;
}

type StreamHandlers = {
  onText?: (content: string) => void;
  onToolStatus?: (content: string) => void;
  onConfidence?: (score: number, explanation: string) => void;
  onImportance?: (tier: number, label: string, needsReview: boolean) => void;
  onFinancialReport?: (data: FinancialReportData) => void;
  onDataTable?: (data: DataTableData) => void;
  onChart?: (data: ChartData) => void;
  onTaskOutput?: (data: TaskOutputData) => void;
  onSheetsLink?: (data: SheetsLinkData) => void;
  onDocsLink?: (data: DocsLinkData) => void;
  onReportReady?: (data: ReportReadyData) => void;
  onResultCard?: (data: ResultCardData) => void;
  onDriveSources?: (sources: Record<string, string>) => void;
  // Codex round 10 P2 Bug 2: Plan Mode mid-stream clarification gate.
  // Without this, the card only appears via the terminal `message` event's
  // structured_output — defeating the point of the mid-stream gate.
  onClarificationRequired?: (data: ClarificationData) => void;
  onPreparationProgress?: (data: PreparationProgressData) => void;
  onGroupBreakdown?: (data: GroupBreakdownData) => void;
  onError?: (error: string) => void;
  onMessage?: (message: ChatMessage) => void;
  onToolStart?: (tool_name: string, tool_input: Record<string, unknown>, step: number) => void;
  onToolEnd?: (tool_name: string, step: number, duration_ms: number, success: boolean, result_summary: string) => void;
};

export function normalizeStreamMessage(raw: Record<string, unknown>): ChatMessage | null {
  const content = raw.content;
  const role = raw.role;

  if (typeof content !== "string" || typeof role !== "string") {
    return null;
  }

  return {
    id: typeof raw.id === "string" ? raw.id : `stream-${Date.now()}`,
    role: (role === "assistant" || role === "user" || role === "system" ? role : "assistant") as ChatMessage["role"],
    content,
    tool_calls: Array.isArray(raw.tool_calls) ? raw.tool_calls : null,
    citations: Array.isArray(raw.citations) ? raw.citations : null,
    created_at:
      typeof raw.created_at === "string" ? raw.created_at : new Date().toISOString(),
    input_tokens: typeof raw.input_tokens === "number" ? raw.input_tokens : undefined,
    output_tokens: typeof raw.output_tokens === "number" ? raw.output_tokens : undefined,
    cache_creation_tokens: typeof raw.cache_creation_tokens === "number" ? raw.cache_creation_tokens : undefined,
    cache_read_tokens: typeof raw.cache_read_tokens === "number" ? raw.cache_read_tokens : undefined,
    model_used: typeof raw.model_used === "string" ? raw.model_used : undefined,
    provider_used: typeof raw.provider_used === "string" ? raw.provider_used : undefined,
    is_byok: typeof raw.is_byok === "boolean" ? raw.is_byok : undefined,
    confidence_score: typeof raw.confidence_score === "number" ? raw.confidence_score : undefined,
    query_importance: typeof raw.query_importance === "number" ? raw.query_importance : undefined,
    structured_output:
      raw.structured_output && typeof raw.structured_output === "object"
        ? (raw.structured_output as ChatMessage["structured_output"])
        : undefined,
  };
}

export function parseSseBuffer(buffer: string): ParsedSseBuffer {
  const events: ChatStreamEvent[] = [];
  const chunks = buffer.split("\n\n");
  const remainder = chunks.pop() || "";

  for (const chunk of chunks) {
    const dataLines = chunk
      .split("\n")
      .filter((line) => line.startsWith("data: "))
      .map((line) => line.slice(6).trim())
      .filter(Boolean);

    if (dataLines.length === 0) {
      continue;
    }

    const dataStr = dataLines.join("\n");

    try {
      const data = JSON.parse(dataStr) as Record<string, unknown>;
      const event = normalizeStreamEvent(data);
      if (event) {
        events.push(event);
      }
    } catch (error) {
      console.error("Failed to parse SSE payload", error);
    }
  }

  return { events, remainder };
}

export async function consumeChatStream(
  response: Response,
  handlers: StreamHandlers,
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Stream not available");
  }

  const decoder = new TextDecoder();
  let buffer = "";
  // Terminal events — once dispatched, we stop reading the stream so the UI
  // never hangs waiting for a sentinel that may be dropped by the proxy.
  //   `message` fires at the end of every successful turn
  //   `error`   fires when the backend gives up on this turn
  let terminalSeen = false;

  try {
    while (!terminalSeen) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSseBuffer(buffer);
      buffer = parsed.remainder;

      for (const event of parsed.events) {
        if (event.type === "text") {
          handlers.onText?.(event.content);
        } else if (event.type === "tool_status") {
          handlers.onToolStatus?.(event.content);
        } else if (event.type === "confidence") {
          handlers.onConfidence?.(event.score, event.explanation);
        } else if (event.type === "importance") {
          handlers.onImportance?.(event.tier, event.label, event.needs_review);
        } else if (event.type === "financial_report") {
          handlers.onFinancialReport?.(event.data);
        } else if (event.type === "data_table") {
          handlers.onDataTable?.(event.data);
        } else if (event.type === "chart") {
          handlers.onChart?.(event.data);
        } else if (event.type === "task_output") {
          handlers.onTaskOutput?.(event.data);
        } else if (event.type === "sheets_link") {
          handlers.onSheetsLink?.(event.data);
        } else if (event.type === "docs_link") {
          handlers.onDocsLink?.(event.data);
        } else if (event.type === "report_ready") {
          handlers.onReportReady?.(event.data);
        } else if (event.type === "result_card") {
          handlers.onResultCard?.(event.data);
        } else if (event.type === "drive_sources") {
          handlers.onDriveSources?.(event.sources);
        } else if (event.type === "clarification_required") {
          handlers.onClarificationRequired?.(event.data);
        } else if (event.type === "preparation_progress") {
          handlers.onPreparationProgress?.(event.data);
        } else if (event.type === "group_breakdown") {
          handlers.onGroupBreakdown?.(event.data);
        } else if (event.type === "error") {
          handlers.onError?.(event.error);
          terminalSeen = true;
        } else if (event.type === "message") {
          handlers.onMessage?.(event.message);
          terminalSeen = true;
        } else if (event.type === "tool_start") {
          handlers.onToolStart?.(event.tool_name, event.tool_input, event.step);
        } else if (event.type === "tool_end") {
          handlers.onToolEnd?.(event.tool_name, event.step, event.duration_ms, event.success, event.result_summary);
        }
      }
    }
  } finally {
    // Release the HTTP connection so the browser's pending fetch resolves.
    try {
      await reader.cancel();
    } catch {
      // Cancel errors are non-fatal
    }
  }
}

export function normalizeStreamEvent(data: Record<string, unknown>): ChatStreamEvent | null {
  const type = data.type;

  if (type === "text" && typeof data.content === "string") {
    return { type, content: data.content };
  }
  if (type === "tool_status" && typeof data.content === "string") {
    return { type, content: data.content };
  }
  if (type === "confidence" && typeof data.score === "number") {
    return { type, score: data.score, explanation: String(data.explanation || "") };
  }
  if (type === "importance" && typeof data.tier === "number") {
    return {
      type,
      tier: data.tier,
      label: String(data.label || ""),
      needs_review: Boolean(data.needs_review),
    };
  }
  if (type === "financial_report" && data.data && typeof data.data === "object") {
    const d = data.data as Record<string, unknown>;
    return {
      type,
      data: {
        report_type: String(d.report_type || ""),
        period: String(d.period || ""),
        columns: Array.isArray(d.columns) ? d.columns : [],
        rows: Array.isArray(d.rows) ? d.rows : [],
        summary: (d.summary && typeof d.summary === "object" ? d.summary : {}) as Record<string, any>,
      },
    };
  }
  if (type === "data_table" && data.data && typeof data.data === "object") {
    const d = data.data as Record<string, unknown>;
    return {
      type,
      data: {
        columns: Array.isArray(d.columns) ? d.columns : [],
        rows: Array.isArray(d.rows) ? d.rows : [],
        row_count: typeof d.row_count === "number" ? d.row_count : 0,
        query: typeof d.query === "string" ? d.query : "",
        truncated: Boolean(d.truncated),
        // Derive isMetric via the shared helper so the live-stream path and the
        // hydration path (page.tsx) cannot drift. metric_compute sets suppress_llm_value
        // to signal that `query` is a metric key (not SQL) and the LLM should not narrate
        // the value. The FE uses isMetric to hide SQL-specific affordances.
        isMetric: deriveDataTableIsMetric(d),
        // The honesty channel (spec docs/superpowers/specs/2026-09-04-celigo-chat-access.md
        // §8) — absent for every data_table tool that doesn't set it.
        ...(Array.isArray(d.caveats) && d.caveats.length > 0 ? { caveats: d.caveats as string[] } : {}),
        ...(typeof d.result_id === "string" ? { result_id: d.result_id } : {}),
      },
    };
  }
  if (type === "chart" && data.data && typeof data.data === "object") {
    const d = data.data as Record<string, unknown>;
    return {
      type,
      data: {
        chart_type: String(d.chart_type || "bar"),
        title: String(d.title || ""),
        subtitle: typeof d.subtitle === "string" ? d.subtitle : undefined,
        x_axis: (d.x_axis && typeof d.x_axis === "object" ? d.x_axis : { label: "", key: "" }) as ChartData["x_axis"],
        y_axes: Array.isArray(d.y_axes) ? d.y_axes : [],
        data: Array.isArray(d.data) ? d.data : [],
        options: (d.options && typeof d.options === "object" ? d.options : undefined) as ChartData["options"],
      } as ChartData,
    };
  }
  if (type === "task_output" && data.data && typeof data.data === "object") {
    const d = data.data as Record<string, unknown>;
    return {
      type,
      data: {
        sku_count: typeof d.sku_count === "number" ? d.sku_count : 0,
        currency_count: typeof d.currency_count === "number" ? d.currency_count : 0,
        output_files: (d.output_files && typeof d.output_files === "object" ? d.output_files : {}) as Record<string, string>,
        preview: Array.isArray(d.preview) ? d.preview : [],
        template_mode: Boolean(d.template_mode),
      },
    };
  }
  if (type === "sheets_link" && data.data && typeof data.data === "object") {
    const d = data.data as Record<string, unknown>;
    return {
      type,
      data: {
        url: String(d.url || ""),
        spreadsheet_id: String(d.spreadsheet_id || ""),
        title: String(d.title || "Spreadsheet"),
        shared_with: typeof d.shared_with === "string" ? d.shared_with : null,
      },
    };
  }
  if (type === "docs_link" && data.data && typeof data.data === "object") {
    const d = data.data as Record<string, unknown>;
    return {
      type,
      data: {
        url: String(d.url || ""),
        doc_id: String(d.doc_id || ""),
        title: String(d.title || "Document"),
        shared_with: typeof d.shared_with === "string" ? d.shared_with : null,
      },
    };
  }
  if (type === "report_ready" && data.data && typeof data.data === "object") {
    const d = data.data as Record<string, unknown>;
    return { type, data: {
      report_id: String(d.report_id || ""),
      title: String(d.title || "Report"),
      url: String(d.url || ""),
      section_count: typeof d.section_count === "number" ? d.section_count : undefined,
    } };
  }
  if (type === "result_card") {
    const card = coerceResultCard(data.data);
    if (card) return { type, data: card };
  }
  if (type === "drive_sources" && data.sources && typeof data.sources === "object") {
    return { type, sources: data.sources as Record<string, string> };
  }
  // Plan Mode mid-stream clarification gate event. Backend emits
  //   {type: "clarification_required", data: ClarificationData}
  // when the model calls the `clarify` tool. Surface as a typed event so
  // the chat UI can render the card immediately rather than wait for the
  // terminal `message` event + session refetch.
  if (type === "clarification_required" && data.data && typeof data.data === "object") {
    return { type, data: data.data as ClarificationData };
  }
  if (type === "preparation_progress" && data.data && typeof data.data === "object") {
    const progress = data.data as PreparationProgressData;
    if (
      typeof progress.checked === "number" &&
      typeof progress.total === "number" &&
      Array.isArray(progress.set_aside) &&
      Array.isArray(progress.now)
    ) {
      return { type, data: progress };
    }
    return null;
  }
  if (type === "group_breakdown") {
    return isGroupBreakdown(data.data) ? { type, data: data.data } : null;
  }
  if (type === "error" && typeof data.error === "string") {
    return { type, error: data.error };
  }
  if (type === "message" && data.message && typeof data.message === "object") {
    const message = normalizeStreamMessage(data.message as Record<string, unknown>);
    if (message) {
      return { type, message };
    }
  }
  if (type === "tool_start" && data.tool_name) {
    return { type, tool_name: String(data.tool_name), tool_input: (data.tool_input && typeof data.tool_input === "object" ? data.tool_input : {}) as Record<string, unknown>, step: typeof data.step === "number" ? data.step : 0 };
  }
  if (type === "tool_end" && data.tool_name) {
    return { type, tool_name: String(data.tool_name), step: typeof data.step === "number" ? data.step : 0, duration_ms: typeof data.duration_ms === "number" ? data.duration_ms : 0, success: data.success !== false, result_summary: String(data.result_summary || "") };
  }

  return null;
}
