import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeAll } from "vitest";
import React from "react";
import { MessageList } from "../message-list";

vi.mock("@/lib/api-client", () => ({
  apiClient: {
    get: vi.fn(async () => ({})),
    post: vi.fn(async () => ({})),
    put: vi.fn(async () => ({})),
    patch: vi.fn(async () => ({})),
    delete: vi.fn(async () => ({})),
  },
}));

vi.mock("@/providers/auth-provider", () => ({
  useAuth: () => ({ user: null }),
}));

// jsdom doesn't implement scrollIntoView or ResizeObserver — stub them so
// MessageList's useLayoutEffect/ResizeObserver setup doesn't throw.
beforeAll(() => {
  if (!("scrollIntoView" in HTMLElement.prototype)) {
    // already missing — define a noop
  }
  HTMLElement.prototype.scrollIntoView = vi.fn();
  (global as { ResizeObserver?: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

function renderWithQueryClient(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

describe("MessageList — ClarificationCard rendering", () => {
  it("renders ClarificationCard when message has type=clarification structured_output", () => {
    const messages = [
      {
        id: "msg-1",
        role: "user",
        content: "What's our revenue?",
        created_at: "2026-04-28T17:00:00Z",
      },
      {
        id: "msg-2",
        role: "assistant",
        content: "",
        structured_output: {
          type: "clarification",
          status: "pending",
          options: [
            { id: "A", title: "NetSuite GL", rationale: "GL", source: "netsuite", is_default: true },
            { id: "B", title: "BigQuery", rationale: "checkout", source: "bigquery", is_default: false },
          ],
          default_id: "A",
          ambiguity_summary: "Revenue can mean two things.",
          confirmation_token: "deadbeef",
          expires_at: "2026-04-28T18:00:00Z",
        },
        created_at: "2026-04-28T17:00:01Z",
      },
    ];
    renderWithQueryClient(
      <MessageList
        messages={messages as unknown as Parameters<typeof MessageList>[0]["messages"]}
        isLoading={false}
        onClarificationChoose={() => {}}
      />,
    );
    expect(screen.getByText(/Pick a definition/)).toBeInTheDocument();
    expect(screen.getByText(/Revenue can mean two things/)).toBeInTheDocument();
  });

  it("renders chosen state for resolved clarification", () => {
    const messages = [
      {
        id: "msg-1",
        role: "assistant",
        content: "",
        structured_output: {
          type: "clarification",
          status: "chosen",
          options: [
            { id: "A", title: "NetSuite GL", rationale: "GL", source: "netsuite", is_default: true },
          ],
          default_id: "A",
          chosen_id: "A",
          ambiguity_summary: "...",
          confirmation_token: "x",
          expires_at: "x",
        },
        created_at: "x",
      },
    ];
    renderWithQueryClient(
      <MessageList
        messages={messages as unknown as Parameters<typeof MessageList>[0]["messages"]}
        isLoading={false}
        onClarificationChoose={() => {}}
      />,
    );
    expect(screen.getByText(/NetSuite GL/)).toBeInTheDocument();
  });
});
