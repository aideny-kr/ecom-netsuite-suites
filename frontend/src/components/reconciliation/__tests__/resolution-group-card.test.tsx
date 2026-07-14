import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ResolutionGroupCard } from "@/components/reconciliation/resolution-group-card";
import type { ReconResolutionGroup } from "@/lib/types";

const feeGroup: ReconResolutionGroup = {
  group_key: "fees:book_fee_line:deposit",
  root_cause: "fees",
  action: "book_fee_line",
  booking_vehicle: "deposit",
  currency: "USD",
  count: 212,
  proposed_count: 212,
  approved_count: 0,
  total_amount: "1284.55",
  above_materiality_count: 3,
};

const base = {
  onApprove: vi.fn(),
  onReject: vi.fn(),
  isApproving: false,
  expanded: false,
  onToggleExpand: vi.fn(),
};

describe("ResolutionGroupCard", () => {
  it("renders count, amount, and the booking-vehicle chip", () => {
    render(<ResolutionGroupCard group={feeGroup} {...base} />);
    expect(screen.getByText(/212 items/i)).toBeInTheDocument();
    expect(screen.getByText(/\$1,284\.55/)).toBeInTheDocument();
    expect(screen.getByText(/deposit fee line/i)).toBeInTheDocument();
  });

  it("shows the materiality split note when items are above threshold", () => {
    render(<ResolutionGroupCard group={feeGroup} {...base} />);
    expect(screen.getByText(/3 above materiality/i)).toBeInTheDocument();
  });

  it("renders the JE fallback chip as flagged (amber)", () => {
    const je = { ...feeGroup, group_key: "fx_rounding:writeoff_je:journalentry",
      root_cause: "fx_rounding", action: "writeoff_je", booking_vehicle: "journalentry" };
    render(<ResolutionGroupCard group={je} {...base} />);
    const chip = screen.getByText(/journal entry/i);
    expect(chip.className).toContain("amber");
  });

  it("approves with the typed note (only sub-materiality by default)", () => {
    const onApprove = vi.fn();
    render(<ResolutionGroupCard group={feeGroup} {...base} onApprove={onApprove} />);
    fireEvent.change(screen.getByPlaceholderText(/note/i), { target: { value: "close" } });
    fireEvent.click(screen.getByRole("button", { name: /approve 209/i }));
    expect(onApprove).toHaveBeenCalledWith("close", []);
  });

  it("needs_human groups have no approve button", () => {
    const nh = { ...feeGroup, group_key: "chargeback:needs_human:none",
      root_cause: "chargeback", action: "needs_human", booking_vehicle: "none" };
    render(<ResolutionGroupCard group={nh} {...base} />);
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.getByText(/review individually/i)).toBeInTheDocument();
  });

  it("carry_forward groups say acknowledge, not approve", () => {
    const cf = { ...feeGroup, group_key: "timing:carry_forward:none",
      root_cause: "timing", action: "carry_forward", booking_vehicle: "none",
      above_materiality_count: 0 };
    render(<ResolutionGroupCard group={cf} {...base} />);
    expect(screen.getByRole("button", { name: /acknowledge/i })).toBeInTheDocument();
  });

  it("formats the amount in the group's own currency (EUR), not hardcoded USD", () => {
    const eurGroup = { ...feeGroup, currency: "EUR", total_amount: "500.00" };
    render(<ResolutionGroupCard group={eurGroup} {...base} />);
    expect(screen.getByText(/€500\.00/)).toBeInTheDocument();
  });

  it("renders order-level root-cause labels (missing_in_netsuite, amount_mismatch)", () => {
    const missing = { ...feeGroup, group_key: "missing_in_netsuite:create_and_apply_deposit:deposit",
      root_cause: "missing_in_netsuite", action: "create_and_apply_deposit", booking_vehicle: "deposit" };
    const { rerender } = render(<ResolutionGroupCard group={missing} {...base} />);
    expect(screen.getByText("Missing in NetSuite")).toBeInTheDocument();

    const mismatch = { ...feeGroup, group_key: "amount_mismatch:book_fee_line:deposit",
      root_cause: "amount_mismatch", action: "book_fee_line", booking_vehicle: "deposit" };
    rerender(<ResolutionGroupCard group={mismatch} {...base} />);
    expect(screen.getByText("Amount mismatch")).toBeInTheDocument();
  });
});
