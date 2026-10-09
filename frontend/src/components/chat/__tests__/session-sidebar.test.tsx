import React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
vi.mock("@/hooks/use-saved-queries", () => ({ useSavedQueries: () => ({ data: [] }), useUpdateSavedQuery: () => ({}), useDeleteSavedQuery: () => ({}) }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
import { SessionSidebar } from "../session-sidebar";
const sessions = ["Inventory evidence", "Revenue analysis"].map((title, i) => ({ id: String(i), title, created_at: "2026-10-08", updated_at: "2026-10-08", is_archived: false }));
function show(extra = {}) {
  return render(<QueryClientProvider client={new QueryClient()}><SessionSidebar sessions={sessions} activeSessionId="0" onSelectSession={vi.fn()} onNewChat={vi.fn()} {...extra} /></QueryClientProvider>);
}
afterEach(cleanup);
describe("conversation history", () => {
  it("filters loaded conversation titles and exposes a clear action", () => {
    show(); fireEvent.change(screen.getByRole("searchbox", { name: "Search loaded conversations" }), { target: { value: "revenue" } });
    expect(screen.queryByRole("button", { name: "Open conversation: Inventory evidence" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open conversation: Revenue analysis" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear conversation search" }));
    expect(screen.getByRole("button", { name: "Open conversation: Inventory evidence" })).toBeInTheDocument();
  });
  it("distinguishes failed history from no conversations and retries", () => {
    const retry = vi.fn(); show({ sessions: [], isError: true, onRetry: retry });
    expect(screen.queryByText("No conversations yet")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Conversation history could not be loaded");
    fireEvent.click(screen.getByRole("button", { name: "Retry conversation history" })); expect(retry).toHaveBeenCalledOnce();
  });
  it("lets an operator load older conversations without changing selection", () => {
    const load = vi.fn(); show({ hasMore: true, onLoadMore: load });
    fireEvent.click(screen.getByRole("button", { name: "Load older conversations" })); expect(load).toHaveBeenCalledOnce();
  });
});
