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

  it("approves with the typed note from the expanded panel", () => {
    const onApprove = vi.fn();
    render(
      <ResolutionGroupsTable
        {...baseProps({ onApprove, expandedKey: "fees:book_fee_line:deposit:USD" })}
      />
    );
    fireEvent.change(screen.getByPlaceholderText(/note/i), { target: { value: "close" } });
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

  it("rejects with the group + currency", () => {
    const onReject = vi.fn();
    render(<ResolutionGroupsTable {...baseProps({ onReject })} />);
    fireEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(onReject).toHaveBeenCalledWith(feeGroup);
  });

  it("disables approve when the one-click count is zero (all sub-materiality already consumed)", () => {
    const zeroed = { ...feeGroup, proposed_count: 3, above_materiality_count: 3 };
    render(<ResolutionGroupsTable {...baseProps({ groups: [zeroed] })} />);
    expect(screen.getByRole("button", { name: /approve 0/i })).toBeDisabled();
  });

  it("disables approve + reject when the disabled prop is set (e.g. closed run)", () => {
    render(<ResolutionGroupsTable {...baseProps({ disabled: true })} />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDisabled();
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
});
