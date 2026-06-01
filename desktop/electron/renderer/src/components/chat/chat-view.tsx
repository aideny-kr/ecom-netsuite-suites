"use client";

// The interactive chat shell (rich-pipe slice 1, Task C3). Wires the composer to
// window.suiteStudio.runAgentStream and renders each streamed event through the
// REUSED chat-stream normalizer: assistant text accumulates into a bubble, a
// data_table event renders the reused data-frame-table card. `done` (which the
// normalizer treats as a non-event) is the terminal signal that re-enables the
// composer; an error event renders an error block.

import { useCallback, useState } from "react";
import { normalizeStreamEvent, type DataTableData } from "@/lib/chat-stream";
import { DataFrameTable } from "@/components/chat/data-frame-table";

type Block =
  | { kind: "user"; text: string }
  | { kind: "assistant_text"; text: string }
  | { kind: "data_table"; data: DataTableData }
  | { kind: "error"; error: string };

function appendText(blocks: Block[], delta: string): Block[] {
  const last = blocks[blocks.length - 1];
  if (last && last.kind === "assistant_text") {
    return [...blocks.slice(0, -1), { kind: "assistant_text", text: last.text + delta }];
  }
  return [...blocks, { kind: "assistant_text", text: delta }];
}

export function ChatView() {
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const query = input.trim();
      if (!query || busy) return;
      setInput("");
      setBlocks((prev) => [...prev, { kind: "user", text: query }]);

      const bridge = window.suiteStudio;
      if (!bridge?.runAgentStream) {
        setBlocks((prev) => [...prev, { kind: "error", error: "Desktop bridge unavailable" }]);
        return;
      }

      setBusy(true);
      bridge.runAgentStream(query, (raw) => {
        const event = normalizeStreamEvent(raw as Record<string, unknown>);
        if (!event) {
          // The normalizer returns null for the desktop-local `done` terminal
          // marker; that's our signal to re-enable the composer.
          if ((raw as { type?: unknown }).type === "done") setBusy(false);
          return;
        }
        if (event.type === "text") {
          setBlocks((prev) => appendText(prev, event.content));
        } else if (event.type === "data_table") {
          setBlocks((prev) => [...prev, { kind: "data_table", data: event.data }]);
        } else if (event.type === "error") {
          setBlocks((prev) => [...prev, { kind: "error", error: event.error }]);
          setBusy(false);
        } else {
          // The reused normalizer can return many event types (tool_status,
          // confidence, chart, message, …) that this slice does not yet render.
          // Surface the drop so a future silent loss becomes visible instead of
          // vanishing into the void.
          console.warn(`unhandled stream event type: ${event.type}`);
        }
      });
    },
    [input, busy],
  );

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col p-6">
      <h1 className="mb-4 text-2xl font-semibold text-foreground">Suite Studio Desktop</h1>

      <div className="flex flex-1 flex-col gap-3 overflow-y-auto" role="log" aria-live="polite">
        {blocks.length === 0 && (
          <p className="text-[15px] text-muted-foreground">
            Ask a question to see rich results stream into the chat.
          </p>
        )}
        {blocks.map((block, i) => {
          if (block.kind === "user") {
            return (
              <div key={i} className="self-end rounded-xl bg-primary px-4 py-2 text-[14px] text-primary-foreground">
                {block.text}
              </div>
            );
          }
          if (block.kind === "assistant_text") {
            return (
              <div key={i} className="self-start whitespace-pre-wrap text-[14px] text-foreground">
                {block.text}
              </div>
            );
          }
          if (block.kind === "data_table") {
            return <DataFrameTable key={i} data={block.data} />;
          }
          return (
            <div key={i} className="self-start rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
              {block.error}
            </div>
          );
        })}
      </div>

      <form onSubmit={handleSubmit} className="mt-4 flex items-center gap-2" aria-label="Send a chat query">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          aria-label="Chat query"
          placeholder="Type a query, e.g. 'show me the demo table'"
          className="flex-1 rounded-md border bg-background px-3 py-2 text-[14px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <button
          type="submit"
          disabled={busy || !input.trim()}
          className="rounded-md bg-primary px-4 py-2 text-[14px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </main>
  );
}
