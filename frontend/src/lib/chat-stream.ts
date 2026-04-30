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

export type StreamBlock =
  | { type: "text"; content: string; id: string }
  | { type: "tool"; tool: StreamingToolCall; id: string }
  | { type: "data_table"; data: DataTableData; id: string }
  | { type: "chart"; data: ChartData; id: string }
  | { type: "financial_report"; data: FinancialReportData; id: string }
  | { type: "task_output"; data: TaskOutputData; id: string }
  | { type: "sheets_link"; data: SheetsLinkData; id: string }
  | { type: "docs_link"; data: DocsLinkData; id: string }
  | { type: "thinking"; content: string; isActive: boolean; id: string }
  | { type: "write_confirmation"; data: WriteConfirmationData; id: string };

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
  | { type: "drive_sources"; sources: Record<string, string> }
  | { type: "chart"; data: ChartData }
  | { type: "clarification_required"; data: ClarificationData }
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
  onDriveSources?: (sources: Record<string, string>) => void;
  // Codex round 10 P2 Bug 2: Plan Mode mid-stream clarification gate.
  // Without this, the card only appears via the terminal `message` event's
  // structured_output — defeating the point of the mid-stream gate.
  onClarificationRequired?: (data: ClarificationData) => void;
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
        } else if (event.type === "drive_sources") {
          handlers.onDriveSources?.(event.sources);
        } else if (event.type === "clarification_required") {
          handlers.onClarificationRequired?.(event.data);
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
