import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { LearnedRulesSection } from "../learned-rules-section";

const createMutate = vi.fn().mockResolvedValue({});
const updateMutate = vi.fn().mockResolvedValue({});
const deleteMutate = vi.fn().mockResolvedValue(undefined);

interface HookState {
  list: { data: unknown; isLoading: boolean; error: unknown };
}
let hookState: HookState;

vi.mock("@/hooks/use-learned-rules", () => ({
  useLearnedRules: () => hookState.list,
  useCreateLearnedRule: () => ({ mutateAsync: createMutate, isPending: false }),
  useUpdateLearnedRule: () => ({ mutateAsync: updateMutate, isPending: false }),
  useDeleteLearnedRule: () => ({ mutateAsync: deleteMutate, isPending: false }),
}));

vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));

const RULE = {
  id: "r1",
  tenant_id: "t1",
  rule_category: "query_logic",
  rule_description: "Count laptops via item.class, never the platform field",
  is_active: true,
  created_by: "u1",
  created_at: "2026-06-04T00:00:00Z",
  updated_at: "2026-06-04T00:00:00Z",
};

function wrap(node: React.ReactNode) {
  return <QueryClientProvider client={new QueryClient()}>{node}</QueryClientProvider>;
}

beforeEach(() => {
  createMutate.mockClear();
  updateMutate.mockClear();
  deleteMutate.mockClear();
  hookState = { list: { data: [RULE], isLoading: false, error: null } };
});

describe("LearnedRulesSection", () => {
  it("renders each rule's description and category", () => {
    render(wrap(<LearnedRulesSection />));
    expect(screen.getByText(/Count laptops via item.class/)).toBeInTheDocument();
    expect(screen.getByText("query_logic")).toBeInTheDocument();
  });

  it("shows an empty state when there are no rules", () => {
    hookState.list = { data: [], isLoading: false, error: null };
    render(wrap(<LearnedRulesSection />));
    expect(screen.getByText(/no learned rules/i)).toBeInTheDocument();
  });

  it("shows a loading state while fetching", () => {
    hookState.list = { data: undefined, isLoading: true, error: null };
    render(wrap(<LearnedRulesSection />));
    expect(screen.getByTestId("learned-rules-loading")).toBeInTheDocument();
  });

  it("toggling a rule's active state calls update with the inverted value", async () => {
    render(wrap(<LearnedRulesSection />));
    fireEvent.click(screen.getByRole("button", { name: /deactivate|active/i }));
    await waitFor(() => expect(updateMutate).toHaveBeenCalledWith({ id: "r1", is_active: false }));
  });

  it("adds a rule via the form", async () => {
    render(wrap(<LearnedRulesSection />));
    fireEvent.click(screen.getByRole("button", { name: /add rule/i }));
    fireEvent.change(screen.getByPlaceholderText(/describe the rule/i), {
      target: { value: "New definition rule" },
    });
    fireEvent.change(screen.getByPlaceholderText(/category/i), { target: { value: "term_definition" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() =>
      expect(createMutate).toHaveBeenCalledWith({
        rule_description: "New definition rule",
        rule_category: "term_definition",
      }),
    );
  });

  it("deletes a rule after confirming in the dialog", async () => {
    render(wrap(<LearnedRulesSection />));
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /delete|remove|confirm/i }));
    await waitFor(() => expect(deleteMutate).toHaveBeenCalledWith("r1"));
  });

  it("paginates 10 rules per page with working prev/next", () => {
    const many = Array.from({ length: 25 }, (_, i) => ({ ...RULE, id: `r${i}`, rule_description: `Rule number ${i}` }));
    hookState.list = { data: many, isLoading: false, error: null };
    render(wrap(<LearnedRulesSection />));

    // Page 1 shows the first 10 only.
    expect(screen.getByText("Rule number 0")).toBeInTheDocument();
    expect(screen.getByText("Rule number 9")).toBeInTheDocument();
    expect(screen.queryByText("Rule number 10")).not.toBeInTheDocument();
    expect(screen.getByText(/page 1 of 3/i)).toBeInTheDocument();

    // Next page shows the next 10.
    fireEvent.click(screen.getByRole("button", { name: /next page/i }));
    expect(screen.getByText("Rule number 10")).toBeInTheDocument();
    expect(screen.queryByText("Rule number 0")).not.toBeInTheDocument();
    expect(screen.getByText(/page 2 of 3/i)).toBeInTheDocument();
  });

  it("hides pagination controls when rules fit on one page", () => {
    render(wrap(<LearnedRulesSection />)); // single rule from beforeEach
    expect(screen.queryByText(/page 1 of/i)).not.toBeInTheDocument();
  });
});
