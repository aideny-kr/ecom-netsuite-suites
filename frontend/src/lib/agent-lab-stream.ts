"use client";

import type { CaseResult } from "@/lib/agent-lab";

/**
 * Agent Lab SSE parser.
 *
 * Backend wire format (from `backend/app/api/v1/agent_lab.py`):
 *   id: <entry>\nevent: <name>\ndata: <json>\n\n
 *
 * Heartbeats omit the `id:` line:
 *   event: heartbeat\ndata: {...}\n\n
 *
 * This parser is distinct from `consumeChatStream` because:
 *   - chat events are keyed by a JSON `.type` discriminator (SSE header lines
 *     discarded);
 *   - agent-lab events are keyed by the SSE native `event:` field, and we
 *     care about the `id:` line for resume-on-reconnect.
 */

export interface AgentLabStreamHandlers {
  onPreparing?: (data: { phase: string; detail?: string }) => void;
  onRunStarted?: (data: {
    total_cases: number;
    estimated_cost_usd?: number;
  }) => void;
  onCaseStarted?: (data: {
    case_id: string;
    question: string;
    index: number;
  }) => void;
  onCaseComplete?: (data: {
    case_id: string;
    result: CaseResult;
    cases_completed: number;
    running_cost_usd: number;
  }) => void;
  onRunComplete?: (data: {
    status: "completed" | "cancelled" | "failed";
    total_cost_usd: number;
    error_message?: string | null;
    summary?: Record<string, unknown>;
  }) => void;
  onError?: (message: string) => void;
  onHeartbeat?: (data: { ts: number }) => void;
  /** Called when an event has an `id:` line. Heartbeats never trigger this. */
  onEventId?: (id: string) => void;
}

interface ParsedSseEvent {
  id: string | null;
  event: string;
  data: string;
}

/**
 * Parse a buffer of SSE text into discrete events. Returns any trailing text
 * that did not terminate in a double-newline so the caller can prepend it to
 * the next chunk.
 */
export function parseAgentLabSseBuffer(buffer: string): {
  events: ParsedSseEvent[];
  remainder: string;
} {
  const events: ParsedSseEvent[] = [];
  const chunks = buffer.split("\n\n");
  const remainder = chunks.pop() ?? "";

  for (const chunk of chunks) {
    if (!chunk) continue;
    let id: string | null = null;
    let event = "message";
    const dataLines: string[] = [];
    for (const line of chunk.split("\n")) {
      if (!line) continue;
      if (line.startsWith(":")) continue; // SSE comment
      if (line.startsWith("id:")) {
        id = line.slice(3).trim();
      } else if (line.startsWith("event:")) {
        event = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        // Per spec, data: fields are joined by \n
        dataLines.push(line.slice(5).trimStart());
      }
    }
    events.push({ id, event, data: dataLines.join("\n") });
  }

  return { events, remainder };
}

/**
 * Read an SSE Response stream and dispatch handlers for each event.
 *
 * Terminal events (`run_complete`, `error`) stop the read. The caller may also
 * abort via the optional AbortSignal; abort exits cleanly without throwing.
 */
export async function consumeAgentLabStream(
  response: Response,
  handlers: AgentLabStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Stream not available");
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let terminal = false;

  // Abort path: cancel the reader so the next read() resolves quickly.
  const onAbort = () => {
    reader.cancel().catch(() => {
      /* non-fatal */
    });
  };
  if (signal) {
    if (signal.aborted) {
      await reader.cancel().catch(() => {});
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });
  }

  try {
    while (!terminal) {
      let value: Uint8Array | undefined;
      let done = false;
      try {
        const read = await reader.read();
        value = read.value;
        done = read.done;
      } catch (err) {
        // If the caller aborted, exit cleanly. Otherwise re-throw.
        if (signal?.aborted) return;
        throw err;
      }

      if (signal?.aborted) return;
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const parsed = parseAgentLabSseBuffer(buffer);
      buffer = parsed.remainder;

      for (const evt of parsed.events) {
        // Track id for resume-on-reconnect. Heartbeats carry no id.
        if (evt.id) handlers.onEventId?.(evt.id);

        let data: unknown = null;
        if (evt.data) {
          try {
            data = JSON.parse(evt.data);
          } catch {
            // Ignore malformed payloads — keep the stream alive.
            continue;
          }
        }

        switch (evt.event) {
          case "preparing":
            handlers.onPreparing?.(
              data as { phase: string; detail?: string },
            );
            break;
          case "run_started":
            handlers.onRunStarted?.(
              data as { total_cases: number; estimated_cost_usd?: number },
            );
            break;
          case "case_started":
            handlers.onCaseStarted?.(
              data as { case_id: string; question: string; index: number },
            );
            break;
          case "case_complete":
            handlers.onCaseComplete?.(
              data as {
                case_id: string;
                result: CaseResult;
                cases_completed: number;
                running_cost_usd: number;
              },
            );
            break;
          case "run_complete":
            handlers.onRunComplete?.(
              data as {
                status: "completed" | "cancelled" | "failed";
                total_cost_usd: number;
                error_message?: string | null;
                summary?: Record<string, unknown>;
              },
            );
            terminal = true;
            break;
          case "error": {
            const msg =
              data && typeof data === "object" && "error" in data
                ? String((data as { error: unknown }).error)
                : "stream error";
            handlers.onError?.(msg);
            terminal = true;
            break;
          }
          case "heartbeat":
            handlers.onHeartbeat?.(data as { ts: number });
            break;
          default:
            // Unknown event name — ignore but keep streaming.
            break;
        }

        if (terminal) break;
      }
    }
  } finally {
    if (signal) signal.removeEventListener("abort", onAbort);
    try {
      await reader.cancel();
    } catch {
      // Cancel errors are non-fatal
    }
  }
}
