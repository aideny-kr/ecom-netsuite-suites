import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { MemoryGraphCanvas } from "../memory-graph-canvas";
import type { MemoryGraph } from "@/hooks/use-memory-graph";

const updateMutate = vi.fn();

vi.mock("@/hooks/use-memory-graph", async () => {
  const actual = await vi.importActual<typeof import("@/hooks/use-memory-graph")>(
    "@/hooks/use-memory-graph",
  );
  return {
    ...actual,
    useUpdateConceptReview: () => ({ mutate: updateMutate, isPending: false }),
  };
});

const PENDING_CONCEPT = {
  id: "c1",
  tenant_id: "t1",
  name: "Laptop 13",
  summary: "Count laptops via item.class, never the platform field.",
  concept_type: "definition",
  review_state: "pending" as const,
  confidence: 0.9,
  confirmed_by: null,
  merged_into_id: null,
  use_count: 0,
  created_at: "2026-06-15T00:00:00Z",
  updated_at: "2026-06-15T00:00:00Z",
};

const GRAPH: MemoryGraph = {
  concepts: [PENDING_CONCEPT],
  edges: [],
};

function wrap(node: React.ReactNode) {
  return <QueryClientProvider client={new QueryClient()}>{node}</QueryClientProvider>;
}

beforeEach(() => {
  updateMutate.mockClear();
});

describe("MemoryGraphCanvas", () => {
  it("renders the canvas test id", () => {
    render(wrap(<MemoryGraphCanvas graph={GRAPH} />));
    expect(screen.getByTestId("memory-graph-canvas")).toBeInTheDocument();
  });

  it("renders a pending concept's name and type", () => {
    render(wrap(<MemoryGraphCanvas graph={GRAPH} />));
    expect(screen.getByText("Laptop 13")).toBeInTheDocument();
    expect(screen.getByText("definition")).toBeInTheDocument();
  });

  it("clicking Confirm calls the update mutation with confirmed review_state", async () => {
    render(wrap(<MemoryGraphCanvas graph={GRAPH} />));
    // reactflow wraps each node in a role="button" drag handle, so the inner
    // Confirm <button> is ambiguous by role — target it by its test id.
    fireEvent.click(screen.getByTestId("confirm-concept-c1"));
    await waitFor(() =>
      expect(updateMutate).toHaveBeenCalledWith({ id: "c1", review_state: "confirmed" }),
    );
  });
});
