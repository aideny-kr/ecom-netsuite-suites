import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { PatternsTab } from "../patterns-tab";

vi.mock("@/lib/agent-lab", async () => ({
  listPatterns: vi.fn(async () => [
    {
      id: "1",
      user_question: "What are orders by shipping country?",
      working_sql: "SELECT ...",
      tables_used: ["transaction"],
      success_count: 12,
      last_used_at: "2026-04-15T12:00:00Z",
      created_at: "2026-04-01T00:00:00Z",
    },
  ]),
}));

describe("PatternsTab", () => {
  it("renders patterns table with question and usage", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <PatternsTab />
      </QueryClientProvider>
    );
    await waitFor(() => {
      expect(screen.getByText(/orders by shipping country/i)).toBeInTheDocument();
      // "12" appears in the Uses cell and the footer sum — use getAllByText
      expect(screen.getAllByText(/12/).length).toBeGreaterThan(0);
    });
  });
});
