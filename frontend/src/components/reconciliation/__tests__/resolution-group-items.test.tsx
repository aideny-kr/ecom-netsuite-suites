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
    deposit_transaction_currency: null, deposit_foreign_amount: null, deposit_exchange_rate: null,
    result_status: "pending",
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
    deposit_transaction_currency: null, deposit_foreign_amount: null, deposit_exchange_rate: null,
    result_status: "pending",
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
    deposit_transaction_currency: null, deposit_foreign_amount: null, deposit_exchange_rate: null,
    result_status: "pending",
  },
  {
    id: "p4", run_id: "r1", result_id: "res4",
    root_cause: "fees", action: "book_fee_line", booking_vehicle: "deposit",
    group_key: "fees:book_fee_line:deposit", source: "planner",
    narrative: "FX-marked deposit — booked in EUR at a recorded exchange rate.",
    proposed_amount: "9.00", currency: "USD",
    above_materiality: false, status: "proposed",
    failure_reason: null, correlation_id: null, created_at: "2026-07-06T00:00:00Z",
    order_reference: "R400000001", stripe_charge_id: "ch_fx_rate",
    netsuite_internal_id: "44444", netsuite_record_type: "custdep",
    stripe_amount: "1000.00", netsuite_amount: "991.00", variance_amount: "9.00",
    deposit_transaction_currency: "EUR", deposit_foreign_amount: "827.00", deposit_exchange_rate: "0.9231",
    result_status: "pending",
  },
  {
    id: "p5", run_id: "r1", result_id: "res5",
    root_cause: "fees", action: "book_fee_line", booking_vehicle: "deposit",
    group_key: "fees:book_fee_line:deposit", source: "planner",
    narrative: "FX-marked deposit — no exchange_rate on file, implied from amounts.",
    proposed_amount: "217.94", currency: "USD",
    above_materiality: false, status: "proposed",
    failure_reason: null, correlation_id: null, created_at: "2026-07-06T00:00:00Z",
    order_reference: "R500000002", stripe_charge_id: "ch_fx_implied",
    netsuite_internal_id: "55555", netsuite_record_type: "custdep",
    // Mutation-detecting fixture: the wrong (old) formula netsuite_amount /
    // stripe_amount = 4782.06 / 5000.00 = 0.9564 differs visibly from the
    // correct implied rate netsuite_amount / deposit_foreign_amount =
    // 4782.06 / 6722.37 = 0.7114 — a regression to the old formula fails this.
    stripe_amount: "5000.00", netsuite_amount: "4782.06", variance_amount: "217.94",
    deposit_transaction_currency: "EUR", deposit_foreign_amount: "6722.37", deposit_exchange_rate: null,
    result_status: "pending",
  },
  {
    id: "p6", run_id: "r1", result_id: "res6",
    root_cause: "fees", action: "book_fee_line", booking_vehicle: "deposit",
    group_key: "fees:book_fee_line:deposit", source: "planner",
    narrative: "Same-currency deposit — no FX chip expected.",
    proposed_amount: "5.00", currency: "EUR",
    above_materiality: false, status: "proposed",
    failure_reason: null, correlation_id: null, created_at: "2026-07-06T00:00:00Z",
    order_reference: "R600000003", stripe_charge_id: "ch_same_ccy",
    netsuite_internal_id: "66666", netsuite_record_type: "custdep",
    stripe_amount: "500.00", netsuite_amount: "495.00", variance_amount: "5.00",
    deposit_transaction_currency: "EUR", deposit_foreign_amount: "495.00", deposit_exchange_rate: "1.000000",
    result_status: "pending",
  },
];

// Each row now hosts a RejectMatchControl, which calls useRejectResult() —
// a react-query mutation. Unmocked it throws for want of a QueryClientProvider.
// Reject behaviour itself is covered in reject-on-resolution-surface.test.tsx.
vi.mock("@/hooks/use-reconciliation", () => ({
  useApproveResult: () => ({ mutate: vi.fn(), isPending: false }),
  useRejectResult: () => ({ mutate: vi.fn(), isPending: false }),
}));

const useGroupProposals = vi.fn(
  (_runId?: string | null, _groupKey?: string | null, _currency?: string | null) => ({
    data: proposals,
    isLoading: false,
  })
);
vi.mock("@/hooks/use-resolution", () => ({
  useGroupProposals: (...args: Parameters<typeof useGroupProposals>) => useGroupProposals(...args),
}));

describe("ResolutionGroupItems", () => {
  const base = {
    runId: "r1",
    groupKey: "fees:book_fee_line:deposit",
    currency: "USD",
    tickedAboveIds: [] as string[],
    onTickAbove: vi.fn(),
    onInvestigate: vi.fn(),
  };

  beforeEach(() => {
    // jsdom doesn't implement clipboard
    Object.assign(navigator, { clipboard: { writeText: vi.fn() } });
  });

  it("passes the group's own currency through to useGroupProposals so the expanded panel's items are scoped like the per-group export", () => {
    render(<ResolutionGroupItems {...base} currency="EUR" />);
    expect(useGroupProposals).toHaveBeenCalledWith("r1", "fees:book_fee_line:deposit", "EUR");
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

  describe("FX mark-only chip (Phase C)", () => {
    it("renders a currency + exchange_rate chip when the deposit's transaction currency differs from the proposal currency", () => {
      render(<ResolutionGroupItems {...base} />);
      const row = screen.getByText("R400000001").closest("tr")!;
      expect(within(row).getByText("EUR @ 0.9231")).toBeInTheDocument();
    });

    it("carries the full text in the chip's title attribute, distinguishing a recorded rate", () => {
      render(<ResolutionGroupItems {...base} />);
      expect(screen.getByText("EUR @ 0.9231")).toHaveAttribute(
        "title",
        "Booked in EUR at 0.9231 (recorded rate)"
      );
    });

    it("falls back to the implied rate (netsuite_amount / deposit_foreign_amount, never stripe_amount) when exchange_rate is null", () => {
      render(<ResolutionGroupItems {...base} />);
      const row = screen.getByText("R500000002").closest("tr")!;
      expect(within(row).getByText("EUR ≈ 0.7114")).toBeInTheDocument();
      // Mutation guard: the old (wrong) formula would render 0.9564 here.
      expect(within(row).queryByText(/0\.9564/)).toBeNull();
    });

    it("marks the implied fallback with an honest 'estimated' title, distinct from a recorded rate", () => {
      render(<ResolutionGroupItems {...base} />);
      expect(screen.getByText("EUR ≈ 0.7114")).toHaveAttribute(
        "title",
        "Booked in EUR, ≈0.7114 estimated from booked amounts (no recorded rate)"
      );
    });

    it("shows no chip when the deposit's transaction currency equals the proposal currency", () => {
      render(<ResolutionGroupItems {...base} />);
      const row = screen.getByText("R600000003").closest("tr")!;
      expect(within(row).queryByText(/@/)).toBeNull();
    });

    it("shows no chip when the deposit carries no transaction currency", () => {
      render(<ResolutionGroupItems {...base} />);
      const row = screen.getByText("R946866359").closest("tr")!;
      expect(within(row).queryByText(/@/)).toBeNull();
    });
  });
});
