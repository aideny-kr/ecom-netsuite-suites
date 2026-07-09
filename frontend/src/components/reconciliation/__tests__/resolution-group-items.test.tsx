import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ResolutionGroupItems } from "@/components/reconciliation/resolution-group-items";
import type { ReconResolutionProposal } from "@/lib/types";

const proposals: ReconResolutionProposal[] = [
  {
    id: "p1", run_id: "r1", result_id: "res1",
    root_cause: "fees", action: "book_fee_line", booking_vehicle: "deposit",
    group_key: "fees:book_fee_line:deposit", source: "planner",
    narrative: "Stripe processing fee — book as a fee line.",
    proposed_amount: "3.20", currency: "USD",
    above_materiality: false, status: "proposed",
    failure_reason: null, correlation_id: null, created_at: "2026-07-06T00:00:00Z",
  },
  {
    id: "p2", run_id: "r1", result_id: "res2",
    root_cause: "fees", action: "book_fee_line", booking_vehicle: "deposit",
    group_key: "fees:book_fee_line:deposit", source: "planner",
    narrative: "Large fee variance.",
    proposed_amount: "120.00", currency: "USD",
    above_materiality: true, status: "proposed",
    failure_reason: null, correlation_id: null, created_at: "2026-07-06T00:00:00Z",
  },
];

vi.mock("@/hooks/use-resolution", () => ({
  useGroupProposals: () => ({ data: proposals, isLoading: false }),
}));

describe("ResolutionGroupItems", () => {
  const base = {
    runId: "r1",
    groupKey: "fees:book_fee_line:deposit",
    tickedAboveIds: [] as string[],
    onTickAbove: vi.fn(),
    onInvestigate: vi.fn(),
  };

  it("renders narratives and amounts", () => {
    render(<ResolutionGroupItems {...base} />);
    expect(screen.getByText(/book as a fee line/i)).toBeInTheDocument();
    expect(screen.getByText(/\$120\.00/)).toBeInTheDocument();
  });

  it("only above-materiality rows get an inclusion checkbox", () => {
    render(<ResolutionGroupItems {...base} />);
    expect(screen.getAllByRole("checkbox")).toHaveLength(1);
  });

  it("ticking the checkbox reports the proposal id", () => {
    const onTickAbove = vi.fn();
    render(<ResolutionGroupItems {...base} onTickAbove={onTickAbove} />);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(onTickAbove).toHaveBeenCalledWith("p2", true);
  });
});
