import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import {
  ResolutionGroupsTable,
  NeedsHumanWorksheet,
} from "@/components/reconciliation/resolution-groups-table";
import type { ReconResolutionGroup, ReconResolutionProposal } from "@/lib/types";

// ResolutionGroupItems does its own data fetching via useGroupProposals — Task 4 owns
// its internals. Mocked here so this suite stays focused on the table/expand/approve
// wiring this task owns.
vi.mock("@/components/reconciliation/resolution-group-items", () => ({
  ResolutionGroupItems: (props: { groupKey: string }) => (
    <div data-testid="group-items">{props.groupKey}</div>
  ),
}));

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

function baseProps(overrides: Partial<React.ComponentProps<typeof ResolutionGroupsTable>> = {}) {
  return {
    runId: "r1",
    groups: [feeGroup],
    expandedKey: null,
    onToggleExpand: vi.fn(),
    isApproving: false,
    tickedAboveByGroup: {},
    onTickAbove: vi.fn(),
    groupResetSignals: {},
    onApprove: vi.fn(),
    onReject: vi.fn(),
    onInvestigate: vi.fn(),
    ...overrides,
  };
}

describe("ResolutionGroupsTable", () => {
  it("renders one row per group with items/approved/above-mat/total columns", () => {
    render(<ResolutionGroupsTable {...baseProps()} />);
    const row = screen.getByText(/stripe processing fees/i).closest("tr")!;
    expect(within(row).getByText("212")).toBeInTheDocument();
    expect(within(row).getByText(/\$1,284\.55/)).toBeInTheDocument();
    expect(within(row).getByText(/deposit fee line/i)).toBeInTheDocument();
  });

  it("shows the above-materiality count under the group label", () => {
    render(<ResolutionGroupsTable {...baseProps()} />);
    expect(screen.getByText(/3 above materiality/i)).toBeInTheDocument();
  });

  it("renders the JE fallback vehicle chip as flagged (amber)", () => {
    const je = {
      ...feeGroup,
      group_key: "fx_rounding:writeoff_je:journalentry",
      root_cause: "fx_rounding",
      action: "writeoff_je",
      booking_vehicle: "journalentry",
    };
    render(<ResolutionGroupsTable {...baseProps({ groups: [je] })} />);
    const chip = screen.getByText(/journal entry/i);
    expect(chip.className).toContain("amber");
  });

  it("formats the amount in the group's own currency (EUR), not hardcoded USD", () => {
    const eurGroup = { ...feeGroup, currency: "EUR", total_amount: "500.00" };
    render(<ResolutionGroupsTable {...baseProps({ groups: [eurGroup] })} />);
    expect(screen.getByText(/€500\.00/)).toBeInTheDocument();
  });

  it("renders order-level root-cause labels (missing_in_netsuite, amount_mismatch)", () => {
    const missing = {
      ...feeGroup,
      group_key: "missing_in_netsuite:create_and_apply_deposit:deposit",
      root_cause: "missing_in_netsuite",
      action: "create_and_apply_deposit",
      booking_vehicle: "deposit",
    };
    const { rerender } = render(<ResolutionGroupsTable {...baseProps({ groups: [missing] })} />);
    expect(screen.getByText("Missing in NetSuite")).toBeInTheDocument();

    const mismatch = {
      ...feeGroup,
      group_key: "amount_mismatch:book_fee_line:deposit",
      root_cause: "amount_mismatch",
      action: "book_fee_line",
      booking_vehicle: "deposit",
    };
    rerender(<ResolutionGroupsTable {...baseProps({ groups: [mismatch] })} />);
    expect(screen.getByText("Amount mismatch")).toBeInTheDocument();
  });

  it("carry_forward groups say acknowledge, not approve", () => {
    const cf = {
      ...feeGroup,
      group_key: "timing:carry_forward:none",
      root_cause: "timing",
      action: "carry_forward",
      booking_vehicle: "none",
      above_materiality_count: 0,
    };
    render(<ResolutionGroupsTable {...baseProps({ groups: [cf] })} />);
    expect(screen.getByRole("button", { name: /acknowledge/i })).toBeInTheDocument();
  });

  it("toggles expand-in-place per cardKey and hosts ResolutionGroupItems when expanded", () => {
    const onToggleExpand = vi.fn();
    const { rerender } = render(<ResolutionGroupsTable {...baseProps({ onToggleExpand })} />);
    expect(screen.queryByTestId("group-items")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/stripe processing fees/i));
    expect(onToggleExpand).toHaveBeenCalledWith("fees:book_fee_line:deposit:USD");

    rerender(
      <ResolutionGroupsTable
        {...baseProps({ onToggleExpand, expandedKey: "fees:book_fee_line:deposit:USD" })}
      />
    );
    expect(screen.getByTestId("group-items")).toHaveTextContent("fees:book_fee_line:deposit");
  });

  it("approves without a note directly from the collapsed row", () => {
    const onApprove = vi.fn();
    render(<ResolutionGroupsTable {...baseProps({ onApprove })} />);
    fireEvent.click(screen.getByRole("button", { name: /approve 209/i }));
    expect(onApprove).toHaveBeenCalledWith(feeGroup, "", []);
  });

  it("types a note in the expanded panel, then approves via the row's fixed approve button, carrying the note", () => {
    const onApprove = vi.fn();
    render(
      <ResolutionGroupsTable
        {...baseProps({ onApprove, expandedKey: "fees:book_fee_line:deposit:USD" })}
      />
    );
    fireEvent.change(screen.getByPlaceholderText(/note/i), { target: { value: "close" } });
    // Exactly one approve button exists — the row's — so this also proves the
    // expanded detail panel does not host a second Approve affordance.
    expect(screen.getAllByRole("button", { name: /approve 209/i })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /approve 209/i }));
    expect(onApprove).toHaveBeenCalledWith(feeGroup, "close", []);
  });

  it("clears the note after a successful approve (resetSignal bump)", () => {
    const { rerender } = render(
      <ResolutionGroupsTable
        {...baseProps({ expandedKey: "fees:book_fee_line:deposit:USD", groupResetSignals: { "fees:book_fee_line:deposit:USD": 0 } })}
      />
    );
    const notes = screen.getByPlaceholderText(/note/i) as HTMLInputElement;
    fireEvent.change(notes, { target: { value: "close" } });
    expect(notes.value).toBe("close");

    rerender(
      <ResolutionGroupsTable
        {...baseProps({ expandedKey: "fees:book_fee_line:deposit:USD", groupResetSignals: { "fees:book_fee_line:deposit:USD": 1 } })}
      />
    );
    expect((screen.getByPlaceholderText(/note/i) as HTMLInputElement).value).toBe("");
  });

  it("rejects with the group + currency (reject lives in the expanded panel)", () => {
    const onReject = vi.fn();
    render(
      <ResolutionGroupsTable
        {...baseProps({ onReject, expandedKey: "fees:book_fee_line:deposit:USD" })}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(onReject).toHaveBeenCalledWith(feeGroup);
  });

  it("disables approve when the one-click count is zero (all sub-materiality already consumed)", () => {
    const zeroed = { ...feeGroup, proposed_count: 3, above_materiality_count: 3 };
    render(<ResolutionGroupsTable {...baseProps({ groups: [zeroed] })} />);
    expect(screen.getByRole("button", { name: /approve 0/i })).toBeDisabled();
  });

  it("disables approve + reject when the disabled prop is set (e.g. closed run)", () => {
    render(
      <ResolutionGroupsTable
        {...baseProps({ disabled: true, expandedKey: "fees:book_fee_line:deposit:USD" })}
      />
    );
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDisabled();
  });

  it("keeps exactly one approve affordance mounted, fixed in the row, whether collapsed or expanded", () => {
    const { rerender } = render(<ResolutionGroupsTable {...baseProps()} />);
    expect(screen.getAllByRole("button", { name: /approve/i })).toHaveLength(1);

    rerender(
      <ResolutionGroupsTable
        {...baseProps({ expandedKey: "fees:book_fee_line:deposit:USD" })}
      />
    );
    expect(screen.getAllByRole("button", { name: /approve/i })).toHaveLength(1);
  });

  it("tells the operator they can tick above-materiality items individually in the item list", () => {
    render(<ResolutionGroupsTable {...baseProps()} />);
    expect(screen.getByText(/tick them individually in the item list/i)).toBeInTheDocument();
  });

  describe("group descriptor subtitle", () => {
    it("shows the muted descriptor for fee-variance groups", () => {
      render(<ResolutionGroupsTable {...baseProps()} />);
      expect(screen.getByText(/stripe fee not booked/i)).toBeInTheDocument();
    });

    it("shows 'deposit not found' for missing_in_netsuite outside carry_forward", () => {
      const missing = {
        ...feeGroup,
        group_key: "missing_in_netsuite:create_and_apply_deposit:deposit",
        root_cause: "missing_in_netsuite",
        action: "create_and_apply_deposit",
      };
      render(<ResolutionGroupsTable {...baseProps({ groups: [missing] })} />);
      expect(screen.getByText(/deposit not found/i)).toBeInTheDocument();
    });

    it("shows 'payout not yet settled' for recency-hold carry_forward groups (missing/missing_in_netsuite root)", () => {
      const recencyHold = {
        ...feeGroup,
        group_key: "missing:carry_forward:none",
        root_cause: "missing",
        action: "carry_forward",
        booking_vehicle: "none",
        above_materiality_count: 0,
      };
      render(<ResolutionGroupsTable {...baseProps({ groups: [recencyHold] })} />);
      expect(screen.getByText(/payout not yet settled/i)).toBeInTheDocument();
    });

    it("shows 'disputed charge' for chargeback groups", () => {
      const cb = {
        ...feeGroup,
        group_key: "chargeback:needs_human:none",
        root_cause: "chargeback",
        action: "needs_human",
        booking_vehicle: "none",
      };
      render(<ResolutionGroupsTable {...baseProps({ groups: [cb] })} />);
      expect(screen.getByText(/disputed charge/i)).toBeInTheDocument();
    });

    it("falls back to no descriptor for an unmapped root cause", () => {
      const other = { ...feeGroup, root_cause: "duplicate", above_materiality_count: 0 };
      render(<ResolutionGroupsTable {...baseProps({ groups: [other] })} />);
      const row = screen.getByText(/duplicate deposits/i).closest("tr")!;
      expect(within(row).queryByText(/—/)).not.toBeInTheDocument();
    });
  });

  it("needs_human groups have no approve/reject row action (defensive — page.tsx filters these out upstream)", () => {
    const nh = {
      ...feeGroup,
      group_key: "chargeback:needs_human:none",
      root_cause: "chargeback",
      action: "needs_human",
      booking_vehicle: "none",
    };
    render(<ResolutionGroupsTable {...baseProps({ groups: [nh] })} />);
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.getByText(/review individually/i)).toBeInTheDocument();
  });

  it("renders the section heading with a group count", () => {
    render(<ResolutionGroupsTable {...baseProps({ groups: [feeGroup, { ...feeGroup, group_key: "timing:carry_forward:none", currency: "EUR" }] })} />);
    expect(screen.getByText(/resolution groups/i)).toBeInTheDocument();
    expect(screen.getByText(/2 groups/i)).toBeInTheDocument();
  });

  it("shows an empty state when there are no non-needs-human groups", () => {
    render(<ResolutionGroupsTable {...baseProps({ groups: [] })} />);
    expect(screen.getByText(/no resolution groups/i)).toBeInTheDocument();
  });
});

describe("NeedsHumanWorksheet", () => {
  const proposal: ReconResolutionProposal = {
    id: "p1",
    run_id: "r1",
    result_id: "res1",
    root_cause: "chargeback",
    action: "needs_human",
    booking_vehicle: "none",
    group_key: "chargeback:needs_human:none",
    source: "planner",
    narrative: "Dispute open at Stripe; policy: never auto-book.",
    proposed_amount: "1940.00",
    currency: "USD",
    above_materiality: true,
    status: "proposed",
    failure_reason: null,
    correlation_id: null,
    created_at: "2026-07-06T00:00:00Z",
    order_reference: "R441209875",
    stripe_charge_id: "ch_3RmFa3Jd",
    netsuite_internal_id: null,
    netsuite_record_type: null,
  };

  beforeEach(() => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn() } });
  });

  it("renders order ref, stripe charge, amount, root cause, and narrative columns", () => {
    render(<NeedsHumanWorksheet proposals={[proposal]} isLoading={false} onInvestigate={vi.fn()} />);
    expect(screen.getByText("R441209875")).toBeInTheDocument();
    expect(screen.getByText("ch_3RmFa3Jd")).toBeInTheDocument();
    expect(screen.getByText(/\$1,940\.00/)).toBeInTheDocument();
    expect(screen.getByText(/chargebacks/i)).toBeInTheDocument();
    expect(screen.getByText(/dispute open at stripe/i)).toBeInTheDocument();
  });

  it("shows a dash for NetSuite ID when there is no linked deposit", () => {
    render(<NeedsHumanWorksheet proposals={[proposal]} isLoading={false} onInvestigate={vi.fn()} />);
    const row = screen.getByText("R441209875").closest("tr")!;
    expect(within(row).getByText("—")).toBeInTheDocument();
  });

  it("fires onInvestigate with the proposal when its button is clicked", () => {
    const onInvestigate = vi.fn();
    render(<NeedsHumanWorksheet proposals={[proposal]} isLoading={false} onInvestigate={onInvestigate} />);
    fireEvent.click(screen.getByText(/investigate in chat/i));
    expect(onInvestigate).toHaveBeenCalledWith(proposal);
  });

  it("copies the raw identifier value when a segment is clicked", () => {
    const writeText = vi.fn();
    Object.assign(navigator, { clipboard: { writeText } });
    render(<NeedsHumanWorksheet proposals={[proposal]} isLoading={false} onInvestigate={vi.fn()} />);
    fireEvent.click(screen.getByText("R441209875"));
    expect(writeText).toHaveBeenCalledWith("R441209875");
  });

  it("shows the empty state when there are no needs-human items", () => {
    render(<NeedsHumanWorksheet proposals={[]} isLoading={false} onInvestigate={vi.fn()} />);
    expect(screen.getByText(/no items need human review/i)).toBeInTheDocument();
  });

  it("shows a loading state", () => {
    render(<NeedsHumanWorksheet proposals={undefined} isLoading={true} onInvestigate={vi.fn()} />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  describe("root-cause chip severity", () => {
    it("colors chargeback/dispute chips critical (red)", () => {
      render(<NeedsHumanWorksheet proposals={[proposal]} isLoading={false} onInvestigate={vi.fn()} />);
      expect(screen.getByText(/chargebacks/i).className).toContain("red");
    });

    it("colors ambiguous-match (amount_mismatch) chips as a warning (amber)", () => {
      const ambiguous = { ...proposal, root_cause: "amount_mismatch" };
      render(<NeedsHumanWorksheet proposals={[ambiguous]} isLoading={false} onInvestigate={vi.fn()} />);
      expect(screen.getByText(/amount mismatch/i).className).toContain("amber");
    });

    it("colors payout-unsettled/missing-deposit chips as a warning (amber)", () => {
      const unsettled = { ...proposal, root_cause: "missing_in_netsuite" };
      render(<NeedsHumanWorksheet proposals={[unsettled]} isLoading={false} onInvestigate={vi.fn()} />);
      expect(screen.getByText(/missing in netsuite/i).className).toContain("amber");
    });

    it("falls back to neutral styling for other root causes", () => {
      const other = { ...proposal, root_cause: "duplicate" };
      render(<NeedsHumanWorksheet proposals={[other]} isLoading={false} onInvestigate={vi.fn()} />);
      const chip = screen.getByText(/duplicate deposits/i);
      expect(chip.className).not.toContain("red");
      expect(chip.className).not.toContain("amber");
    });
  });
});
