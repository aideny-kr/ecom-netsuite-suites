import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within, waitFor } from "@testing-library/react";
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
    stripe_amount: "3.20", netsuite_amount: "3.15", variance_amount: "0.05",
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
    stripe_amount: "120.00", netsuite_amount: null, variance_amount: null,
  },
  {
    id: "p3", run_id: "r1", result_id: "res3",
    root_cause: "amount_mismatch", action: "needs_human", booking_vehicle: "none",
    group_key: "amount_mismatch:needs_human:none", source: "planner",
    narrative: "Ambiguous match — several open deposits share this amount and date, needs a human to pick the right one before booking anything.",
    proposed_amount: "45.00", currency: "USD",
    above_materiality: false, status: "proposed",
    failure_reason: null, correlation_id: null, created_at: "2026-07-06T00:00:00Z",
    order_reference: "R555000111", stripe_charge_id: "ch_needs_human",
    netsuite_internal_id: null, netsuite_record_type: null,
    stripe_amount: "45.00", netsuite_amount: null, variance_amount: null,
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
    expect(screen.getByText("$120.00")).toBeInTheDocument();
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
    const unmatchedRow = screen.getByText("R123456789").closest("tr")!;
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

  it("does not show a copied checkmark when the clipboard write fails", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    Object.assign(navigator, { clipboard: { writeText } });
    const { container } = render(<ResolutionGroupItems {...base} />);
    fireEvent.click(screen.getByText("NS#12345"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("12345"));
    expect(container.querySelector(".text-green-500")).toBeNull();
  });

  it("renders the Stripe amt, NetSuite amt, and Variance columns from the proposal's amounts", () => {
    render(<ResolutionGroupItems {...base} />);
    const row1 = screen.getByText("R946866359").closest("tr")!;
    expect(within(row1).getByText("$3.20")).toBeInTheDocument();
    expect(within(row1).getByText("$3.15")).toBeInTheDocument();
    expect(within(row1).getByText("$0.05")).toBeInTheDocument();
  });

  it("renders — for null NetSuite amt and Variance when there is no matched result", () => {
    render(<ResolutionGroupItems {...base} />);
    const row2 = screen.getByText("R123456789").closest("tr")!;
    // NetSuite ID, NetSuite amt, and Variance are all null on this row.
    expect(within(row2).getAllByText("—")).toHaveLength(3);
  });

  it("renders a status chip and a materiality chip per row", () => {
    render(<ResolutionGroupItems {...base} />);
    const row1 = screen.getByText("R946866359").closest("tr")!;
    expect(within(row1).getByText("Proposed")).toBeInTheDocument();
    expect(within(row1).getByText("Within materiality")).toBeInTheDocument();
    const row2 = screen.getByText("R123456789").closest("tr")!;
    expect(within(row2).getByText("Above materiality")).toBeInTheDocument();
  });

  it("truncates the narrative with a title attribute carrying the full text", () => {
    render(<ResolutionGroupItems {...base} />);
    const cell = screen.getByTitle(proposals[2].narrative);
    expect(cell.className).toContain("truncate");
  });

  it("shows the Investigate-in-chat button only on needs_human rows", () => {
    const onInvestigate = vi.fn();
    render(<ResolutionGroupItems {...base} onInvestigate={onInvestigate} />);
    const buttons = screen.getAllByRole("button", { name: /investigate in chat/i });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(onInvestigate).toHaveBeenCalledWith(proposals[2]);
  });
});
