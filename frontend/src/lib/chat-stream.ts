"use client";

import type { ChatMessage } from "@/lib/types";

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

export type ChatStreamEvent =
  | { type: "text"; content: string }
  | { type: "tool_status"; content: string }
  | { type: "confidence"; score: number; explanation: string }
  | { type: "importance"; tier: number; label: string; needs_review: boolean }
  | { type: "financial_report"; data: FinancialReportData }
  | { type: "data_table"; data: DataTableData }
  | { type: "error"; error: string }
  | { type: "message"; message: ChatMessage };

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
  onError?: (error: string) => void;
  onMessage?: (message: ChatMessage) => void;
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

  while (true) {
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
      } else if (event.type === "error") {
        handlers.onError?.(event.error);
      } else if (event.type === "message") {
        handlers.onMessage?.(event.message);
      }
    }
  }
}

function normalizeStreamEvent(data: Record<string, unknown>): ChatStreamEvent | null {
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
  if (type === "error" && typeof data.error === "string") {
    return { type, error: data.error };
  }
  if (type === "message" && data.message && typeof data.message === "object") {
    const message = normalizeStreamMessage(data.message as Record<string, unknown>);
    if (message) {
      return { type, message };
    }
  }

  return null;
}
