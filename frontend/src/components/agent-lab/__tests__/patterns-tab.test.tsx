import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { PatternsTab } from "../patterns-tab";

const mockPatterns = vi.fn();
vi.mock("@/lib/agent-lab", () => ({
  listPatterns: (...args: unknown[]) => mockPatterns(...args),
}));

describe("PatternsTab", () => {
  beforeEach(() => {
    mockPatterns.mockReset();
  });

  it("renders patterns table with question and usage", async () => {
    mockPatterns.mockResolvedValue([
      {
        id: "1",
        user_question: "What are orders by shipping country?",
        working_sql: "SELECT ...",
        tables_used: ["transaction"],
        success_count: 12,
        last_used_at: "2026-04-15T12:00:00Z",
        created_at: "2026-04-01T00:00:00Z",
      },
    ]);
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

  it("renders 'never' text for patterns with null last_used_at", async () => {
    mockPatterns.mockResolvedValue([
      {
        id: "p-never",
        user_question: "Inventory aging buckets by warehouse",
        working_sql: "SELECT ...",
        tables_used: null,
        success_count: 0,
        last_used_at: null,
        created_at: "2026-04-15T00:00:00Z",
      },
    ]);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <PatternsTab />
      </QueryClientProvider>
    );
    await waitFor(() => {
      expect(screen.getByText(/inventory aging/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/never/i)).toBeInTheDocument();
  });
});
