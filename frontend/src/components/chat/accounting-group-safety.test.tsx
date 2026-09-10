import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeAll } from "vitest";
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

import type { WriteConfirmationData } from "@/lib/types";
import { WriteConfirmationCard } from "@/components/chat/write-confirmation-card";
const card: WriteConfirmationData = {
  type: "write_confirmation",
  mutation_type: "update",
  record_type: "invoice",
  record_id: "20",
  proposed_fields: { taxRate: 5 },
  current_record: { taxRate: 4 },
  tool_name: "native_update",
  tool_input: { recordId: "20", data: '{"taxRate":5}' },
  confirmation_token: "signed",
  status: "pending",
  target_account: "123",
  target_environment: "PRODUCTION",
  accounting_review: {
    order_reference: "R123",
    record_id: "20",
    case_id: "case",
    before: {
      total: "104.00",
      taxTotal: "4.00",
      taxRate: 4,
      currency_code: "USD",
      tranId: "INV20",
      subsidiary: { refName: "Example" },
      postingPeriod: { refName: "August" },
    },
    expected_after: { total: "105.00", taxTotal: "5.00" },
    proposed_fields: { taxRate: 5 },
    scope: { netsuite_account_id: "123", subsidiary_id: "1" },
    period: { arLocked: true, allLocked: true },
    tax_item: { taxAgency: { refName: "Existing agency" } },
    tax_account: "210",
    ar_account: "119",
    accounting_book: "1",
    approval_basis:
      "Keep the approved source basis and existing tax classification.",
  },
};

const group = {
  ...card,
  accounting_review: null,
  accounting_group: {
    group_id: "group",
    concurrency: 3,
    members: [
      {
        case_id: "case",
        order_reference: "R123",
        confirmation_id: "child",
        card,
      },
    ],
  },
};

describe("Group approval integration safeguards", () => {
  it("disables repeated approvals while the initial request is outstanding", async () => {
    vi.clearAllMocks();
    api.get.mockImplementation((path: string) => {
      if (path === "/api/v1/chat/sessions")
        return Promise.resolve([sessionSummary()]);
      if (path === `/api/v1/chat/sessions/${SESSION_ID}`)
        return Promise.resolve(
          sessionDetailPayload([
            { ...writeConfirmationMessage(), structured_output: group },
          ]),
        );
      return Promise.resolve([]);
    });
    api.post.mockImplementation((path: string) =>
      path.endsWith("/messages") ? new Promise(() => {}) : Promise.resolve({}),
    );
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox"));
    const button = screen.getByRole("button", { name: /Approve 1 invoice/ });
    fireEvent.click(button);
    expect(api.post.mock.calls.filter(isMessagesCall)).toHaveLength(1);
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(api.post.mock.calls.filter(isMessagesCall)).toHaveLength(1);
  });
  it("keeps observing the first result stream on repeated approval", async () => {
    vi.clearAllMocks();
    api.get.mockImplementation((path: string) => {
      if (path === "/api/v1/chat/sessions")
        return Promise.resolve([sessionSummary()]);
      if (path === `/api/v1/chat/sessions/${SESSION_ID}`)
        return Promise.resolve(
          sessionDetailPayload([
            { ...writeConfirmationMessage(), structured_output: group },
          ]),
        );
      return Promise.resolve([]);
    });
    api.post.mockResolvedValue({ run_id: "run-1" });
    api.streamGet.mockImplementation(
      (_path: string, signal: AbortSignal) =>
        new Promise((_resolve, reject) =>
          signal.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          ),
        ),
    );
    const view = renderPage();
    fireEvent.click(await screen.findByRole("checkbox"));
    const button = screen.getByRole("button", { name: /Approve 1 invoice/ });
    fireEvent.click(button);
    await waitFor(() => expect(api.streamGet).toHaveBeenCalledTimes(1));
    const firstSignal = api.streamGet.mock.calls[0][1] as AbortSignal;
    expect(firstSignal.aborted).toBe(false);
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(api.post.mock.calls.filter(isMessagesCall)).toHaveLength(1);
    expect(firstSignal.aborted).toBe(false);
    view.unmount();
  });
  it("shows group execution context in expanded children", () => {
    render(
      <WriteConfirmationCard
        data={{ ...group, status: "executing" }}
        onConfirm={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("R123"));
    expect(screen.getByText("Running · awaiting results")).toBeInTheDocument();
    expect(screen.queryByText("Awaiting approval")).not.toBeInTheDocument();
    expect(screen.getAllByText("Awaiting result")).toHaveLength(2);
    expect(
      screen.queryByRole("button", { name: /Approve/ }),
    ).not.toBeInTheDocument();
  });
  it("shows a visible parent warning for an indeterminate group", () => {
    render(
      <WriteConfirmationCard
        data={{
          ...group,
          status: "indeterminate",
          accounting_group: {
            ...group.accounting_group,
            members: [
              {
                ...group.accounting_group.members[0],
                card: { ...card, status: "indeterminate" },
                reason:
                  "Batch interrupted. Check this order’s recorded outcome; no automatic retry.",
              },
            ],
          },
        }}
        onConfirm={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Some changes may already have reached NetSuite",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("no automatic retry");
    expect(screen.getAllByText("Outcome unconfirmed")[0]).toBeVisible();
  });
});
