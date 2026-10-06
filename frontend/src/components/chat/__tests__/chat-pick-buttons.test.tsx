// Source pick buttons and follow-up buttons (approved mock option A, 2026-10-01).
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeAll, describe, expect, it, vi } from "vitest";
import React from "react";
import { MessageList } from "../message-list";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn(async () => ({})), post: vi.fn(async () => ({})) },
}));
vi.mock("@/providers/auth-provider", () => ({ useAuth: () => ({ user: null }) }));

beforeAll(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  (global as { ResizeObserver?: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

function renderAnswer(content: string) {
  const onFollowUp = vi.fn();
  const messages = [
    { id: "u1", role: "user", content: "since we launched platform Yucca, how many and how much did we sell?", created_at: "2026-10-01T20:00:00Z" },
    { id: "a1", role: "assistant", content, created_at: "2026-10-01T20:00:05Z", tool_calls: [] },
  ];
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MessageList
        messages={messages as unknown as Parameters<typeof MessageList>[0]["messages"]}
        isLoading={false}
        onFollowUp={onFollowUp}
      />
    </QueryClientProvider>,
  );
  return onFollowUp;
}

describe("pick buttons", () => {
  it("shows the server's source choices as buttons that send the source name", () => {
    const onFollowUp = renderAnswer(
      "Which data source should I use for this question: BigQuery, Metabase or NetSuite?\n\n```sources\nBigQuery\nMetabase\nNetSuite\n```",
    );
    const group = screen.getByTestId("source-picks");
    expect(within(group).getAllByRole("button").map((b) => b.textContent)).toEqual(["BigQuery", "Metabase", "NetSuite"]);
    expect(screen.queryByText(/```sources/)).not.toBeInTheDocument();
    fireEvent.click(within(group).getByRole("button", { name: "NetSuite" }));
    expect(onFollowUp).toHaveBeenCalledWith("NetSuite");
  });

  it("styles source and follow-up buttons alike, in the accent color", () => {
    renderAnswer("Done.\n\n```followups\nBreak down by SKU\n```");
    const button = within(screen.getByTestId("follow-up-chips")).getByRole("button", { name: "Break down by SKU" });
    expect(button.className).toContain("border-primary");
    expect(button.className).toContain("bg-primary/");
  });
});
