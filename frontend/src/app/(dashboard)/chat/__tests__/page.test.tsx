/**
 * Proves the write-confirmation slot-fill wire end to end, not just the card.
 *
 * Task 9 (docs/superpowers/plans/2026-08-19-agentic-netsuite-write-loop.md):
 * write-confirmation-card.test.tsx proves WriteConfirmationCard calls
 * onConfirm(slotValues). That alone does NOT prove the value reaches the
 * server — page.tsx's handleWriteConfirm/handleSend build the actual POST
 * body, and page.tsx is not exercised by the card's own test file. A version
 * of this wire that widens onConfirm's signature but drops slotValues on the
 * floor between message-list.tsx and page.tsx would still pass every card
 * test while silently sending an approve with no slot_values — exactly the
 * failure this plan exists to eliminate, reintroduced at the last hop.
 *
 * This test renders the real ChatPage, drives a real click on the real
 * WriteConfirmationCard's slot input and Approve button, and asserts on the
 * body handed to the mocked apiClient.post — i.e. what would actually have
 * been JSON-serialized onto the wire (api-client.ts's `request()` does
 * `JSON.stringify(body)` with exactly this object).
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import React from "react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
  stream: vi.fn(),
  streamGet: vi.fn(),
  download: vi.fn(),
}));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));

vi.mock("@/providers/auth-provider", () => ({
  useAuth: () => ({ user: { id: "u-1", full_name: "Test User", roles: [] } }),
}));

import ChatPage from "@/app/(dashboard)/chat/page";

const SESSION_ID = "sess-1";
const WRITE_MSG_ID = "msg-write-1";

function makeSseResponse(events: string[] = []): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const ev of events) controller.enqueue(encoder.encode(ev));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function writeConfirmationMessage() {
  return {
    id: WRITE_MSG_ID,
    role: "assistant",
    content: "",
    structured_output: {
      type: "write_confirmation",
      mutation_type: "create",
      record_type: "customer",
      record_id: null,
      proposed_fields: { companyname: "test ai customer" },
      proposed_lines: [],
      current_record: null,
      tool_name: "ext__aaa__ns_createRecord",
      tool_input: {},
      confirmation_token: "tok-1",
      editable_slots: [
        {
          name: "subsidiary",
          label: "Primary Subsidiary",
          type: "select",
          allowed: [{ value: "1", label: "Framework Inc" }],
        },
      ],
      unvalidated: false,
      status: "pending",
    },
    created_at: "2026-08-19T00:00:01Z",
  };
}

function sessionSummary() {
  return {
    id: SESSION_ID,
    title: "Test session",
    is_archived: false,
    created_at: "2026-08-19T00:00:00Z",
    updated_at: "2026-08-19T00:00:00Z",
  };
}

function sessionDetailPayload(messages: unknown[]) {
  return {
    id: SESSION_ID,
    title: "Test session",
    is_archived: false,
    messages,
    created_at: "2026-08-19T00:00:00Z",
    updated_at: "2026-08-19T00:00:00Z",
  };
}

beforeAll(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  (global as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ChatPage />
    </QueryClientProvider>,
  );
}

function isMessagesCall(call: unknown[]): boolean {
  const path = call[0];
  return typeof path === "string" && path.endsWith("/messages");
}

describe("ChatPage — write-confirmation slot_values reach the request body", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.get.mockImplementation((path: string) => {
      if (path === "/api/v1/chat/sessions") return Promise.resolve([sessionSummary()]);
      if (path === `/api/v1/chat/sessions/${SESSION_ID}`) {
        return Promise.resolve(sessionDetailPayload([writeConfirmationMessage()]));
      }
      return Promise.resolve([]);
    });
    api.post.mockImplementation((path: string) => {
      if (path.endsWith("/messages")) return Promise.resolve({ run_id: "run-1" });
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    api.streamGet.mockResolvedValue(makeSseResponse([]));
  });

  it("sends the human-filled slot value as write_confirm.slot_values in the approve request body", async () => {
    renderPage();

    // Wait for session auto-select -> sessionDetail fetch -> the real
    // WriteConfirmationCard to render its real slot <select>.
    const select = await screen.findByLabelText("Primary Subsidiary");
    fireEvent.change(select, { target: { value: "1" } });

    const approveButton = await screen.findByRole("button", { name: /approve/i });
    expect(approveButton).toBeEnabled();
    fireEvent.click(approveButton);

    await waitFor(() => {
      expect(api.post.mock.calls.find(isMessagesCall)).toBeTruthy();
    });

    const messagesCall = api.post.mock.calls.find(isMessagesCall)!;
    const [, body] = messagesCall as [string, { write_confirm?: Record<string, unknown> }];

    // The load-bearing assertion: not just that onConfirm fired, but that the
    // exact object handed to apiClient.post — the object api-client.ts's
    // request() JSON.stringifies straight onto the wire — carries the human's
    // typed value under write_confirm.slot_values, the field name the
    // orchestrator reads (Task 7, orchestrator.py:1759).
    expect(body.write_confirm).toEqual({
      action: "approve",
      confirmation_id: WRITE_MSG_ID,
      slot_values: { subsidiary: "1" },
    });
  });

  it("still sends a plain reject with no slot_values (unchanged behavior)", async () => {
    renderPage();

    const rejectButton = await screen.findByRole("button", { name: /reject/i });
    fireEvent.click(rejectButton);

    await waitFor(() => {
      expect(api.post.mock.calls.find(isMessagesCall)).toBeTruthy();
    });

    const messagesCall = api.post.mock.calls.find(isMessagesCall)!;
    const [, body] = messagesCall as [string, { write_confirm?: Record<string, unknown> }];
    expect(body.write_confirm).toEqual({
      action: "reject",
      confirmation_id: WRITE_MSG_ID,
    });
  });
});
