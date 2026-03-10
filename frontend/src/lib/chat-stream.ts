"use client";

import type { ChatMessage } from "@/lib/types";

export type ChatStreamEvent =
  | { type: "text"; content: string }
  | { type: "tool_status"; content: string }
  | { type: "confidence"; score: number; explanation: string }
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
