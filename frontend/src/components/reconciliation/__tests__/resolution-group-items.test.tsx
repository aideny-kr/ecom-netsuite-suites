import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
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
    order_reference: "R946866359", stripe_charge_id: "ch_3Nxxx",
    netsuite_internal_id: "12345", netsuite_record_type: "custdep",
  },
  {
    id: "p2", run_id: "r1", result_id: "res2",
    root_cause: "fees", action: "book_fee_line", booking_vehicle: "deposit",
    group_key: "fees:book_fee_line:deposit", source: "planner",
    narrative: "Large fee variance.",
    proposed_amount: "120.00", currency: "USD",
    above_materiality: true, status: "proposed",
    failure_reason: null, correlation_id: null, created_at: "2026-07-06T00:00:00Z",
    order_reference: "R123456789", stripe_charge_id: "ch_unmatched",
    netsuite_internal_id: null, netsuite_record_type: null,
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

  beforeEach(() => {
    // jsdom doesn't implement clipboard
    Object.assign(navigator, { clipboard: { writeText: vi.fn() } });
  });

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

  it("renders order ref, Stripe charge id, and NetSuite id when all present", () => {
    render(<ResolutionGroupItems {...base} />);
    expect(screen.getByText("R946866359")).toBeInTheDocument();
    expect(screen.getByText("ch_3Nxxx")).toBeInTheDocument();
    expect(screen.getByText("NS#12345")).toBeInTheDocument();
  });

  it("omits the NetSuite segment when the item has no linked deposit", () => {
    render(<ResolutionGroupItems {...base} />);
    const unmatchedRow = screen.getByText("R123456789").closest("li")!;
    expect(within(unmatchedRow).getByText("ch_unmatched")).toBeInTheDocument();
    expect(within(unmatchedRow).queryByText(/^NS#/)).toBeNull();
  });

  it("copies the raw identifier value (not the display prefix) when a segment is clicked", () => {
    const writeText = vi.fn();
    Object.assign(navigator, { clipboard: { writeText } });
    render(<ResolutionGroupItems {...base} />);
    fireEvent.click(screen.getByText("NS#12345"));
    expect(writeText).toHaveBeenCalledWith("12345");
  });
});
