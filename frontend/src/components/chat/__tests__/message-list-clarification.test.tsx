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
    const future = new Date(Date.now() + 10 * 60 * 1000).toISOString();
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
          // Use a relative future timestamp so this test doesn't go stale
          // when wall-clock time crosses a hardcoded value.
          expires_at: future,
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

  // Codex P2 Bug 3: stale cards must not be actionable. The leaf
  // ClarificationCard supports `expired?: boolean`, but the message-list
  // call site never computes it. Backend now returns 410 on expired
  // resume → user clicks → backend rejects → user confused. Fix: compute
  // `expired` from `data.expires_at` in message-list and pass it down.
  it("renders the expired visual state when expires_at is in the past", () => {
    const messages = [
      {
        id: "msg-1",
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
          ambiguity_summary: "Revenue is ambiguous.",
          confirmation_token: "deadbeef",
          // 2020-01-01 is solidly in the past relative to test time.
          expires_at: "2020-01-01T00:00:00Z",
        },
        created_at: "2020-01-01T00:00:00Z",
      },
    ];
    renderWithQueryClient(
      <MessageList
        messages={messages as unknown as Parameters<typeof MessageList>[0]["messages"]}
        isLoading={false}
        onClarificationChoose={() => {}}
      />,
    );
    // ClarificationCard's expired state shows "This card expired" heading.
    expect(screen.getByText(/This card expired/i)).toBeInTheDocument();
    // The pending UI must NOT render — no "Pick a definition" heading.
    expect(screen.queryByText(/Pick a definition/i)).not.toBeInTheDocument();
  });

  it("renders the pending state when expires_at is in the future", () => {
    const future = new Date(Date.now() + 10 * 60 * 1000).toISOString();
    const messages = [
      {
        id: "msg-1",
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
          ambiguity_summary: "Revenue is ambiguous.",
          confirmation_token: "deadbeef",
          expires_at: future,
        },
        created_at: "2026-04-28T17:00:00Z",
      },
    ];
    renderWithQueryClient(
      <MessageList
        messages={messages as unknown as Parameters<typeof MessageList>[0]["messages"]}
        isLoading={false}
        onClarificationChoose={() => {}}
      />,
    );
    // Pending UI renders, expired UI does not.
    expect(screen.getByText(/Pick a definition/i)).toBeInTheDocument();
    expect(screen.queryByText(/This card expired/i)).not.toBeInTheDocument();
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
