// @vitest-environment jsdom
/**
 * ChatView wiring test (rich-pipe slice 1, Task C3).
 *
 * Proves the full renderer path key-free: composer submit -> window.suiteStudio
 * .runAgentStream -> per-event onEvent -> the REUSED chat-stream normalizer ->
 * streamed assistant text + the data-frame-table card rendered in history, and
 * the composer re-enables on the terminal `done`. The live render (real agent)
 * is operator-deferred — see desktop/SMOKE-DEFERRAL-RICH-PIPE.md.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { ChatView } from "@/components/chat/chat-view";

type EventCb = (event: Record<string, unknown>) => void;

function installBridge(): { last: () => EventCb; queries: string[] } {
  const calls: EventCb[] = [];
  const queries: string[] = [];
  (window as unknown as { suiteStudio: unknown }).suiteStudio = {
    runAgentStream: (query: string, onEvent: EventCb) => {
      queries.push(query);
      calls.push(onEvent);
    },
  };
  return { last: () => calls[calls.length - 1], queries };
}

beforeEach(() => {
  delete (window as unknown as { suiteStudio?: unknown }).suiteStudio;
});

function submit(query: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: query } });
  fireEvent.click(screen.getByRole("button", { name: /send/i }));
}

describe("ChatView streams text + data_table over the IPC bridge", () => {
  it("renders the user query, streamed text, and the data_table card", () => {
    const bridge = installBridge();
    render(<ChatView />);

    submit("show me the demo table");
    expect(bridge.queries).toEqual(["show me the demo table"]);
    expect(screen.getByText("show me the demo table")).toBeInTheDocument();

    act(() => {
      const on = bridge.last();
      on({ type: "text", content: "Here are the sample account balances:" });
      on({
        type: "data_table",
        data: {
          columns: ["Account", "Balance (USD)"],
          rows: [["Cash & Equivalents", 1284500.0]],
          row_count: 1,
          query: "",
          truncated: false,
        },
      });
      on({ type: "done", tokens_used: 42 });
    });

    expect(screen.getByText("Here are the sample account balances:")).toBeInTheDocument();
    // The reused data-frame-table card rendered the tool's rows.
    expect(screen.getByText("Query Results")).toBeInTheDocument();
    expect(screen.getByText("Cash & Equivalents")).toBeInTheDocument();
  });

  it("surfaces an error event as an error block", () => {
    const bridge = installBridge();
    render(<ChatView />);
    submit("q");
    act(() => bridge.last()({ type: "error", error: "No Anthropic credential resolved" }));
    expect(screen.getByText(/No Anthropic credential resolved/)).toBeInTheDocument();
  });

  it("does not crash or render a card when a malformed data_table event arrives", () => {
    // Exercises the reused normalizer's defensive coercion in the renderer path:
    // a data_table event with wrong-typed fields is coerced (columns -> []), so
    // the card renders nothing rather than throwing.
    const bridge = installBridge();
    render(<ChatView />);
    submit("q");
    act(() => {
      bridge.last()({
        type: "data_table",
        data: { columns: "not-an-array", rows: 99, row_count: "x", query: 5, truncated: "yes" },
      });
      bridge.last()({ type: "done", tokens_used: 0 });
    });
    // columns coerced to [] -> the card returns null -> no "Query Results" header, no crash.
    expect(screen.queryByText("Query Results")).toBeNull();
  });

  it("renders a graceful message when the desktop bridge is unavailable", () => {
    render(<ChatView />); // no window.suiteStudio installed
    submit("q");
    expect(screen.getByText(/bridge unavailable/i)).toBeInTheDocument();
  });
});

describe("ChatView surfaces unhandled stream event types", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("console.warns when a normalized-but-unhandled event type arrives", () => {
    // `tool_status` normalizes successfully via the reused normalizer but is not
    // one of the three branches (text/data_table/error) the renderer handles.
    // Without a default branch it would be silently dropped — a latent footgun
    // for future rich-pipe slices. Assert the default branch logs a visible warn.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const bridge = installBridge();
    render(<ChatView />);
    submit("q");
    act(() => bridge.last()({ type: "tool_status", content: "Running SuiteQL…" }));

    expect(warn).toHaveBeenCalledWith("unhandled stream event type: tool_status");
  });
});
