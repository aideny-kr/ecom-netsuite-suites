/**
 * Reject, on the surface a flag-ON tenant actually works in.
 *
 * #193 shipped reject on ReconResultsTable, which page.tsx renders only inside
 * renderClassicBucketView(). For a tenant with recon_resolution_ui ON (Framework, the
 * only tenant with recon data) that path is reachable ONLY behind the "Show all results
 * (classic view)" disclosure — so the primary review flow had no reject control at all.
 * Measured 2026-08-10 on live data: 354,827 results, ZERO dispositions of any kind.
 *
 * WHAT THIS SUITE DOES NOT CLAIM. Measured the same day: of 46,480 resolution proposals,
 * ZERO sit over an envelope-grade result. resolution_planner rule 2 skips "clean
 * deterministic match, zero variance" outright — and that is precisely the envelope's
 * admission ladder (_is_envelope_eligible). So every reject taken on THIS surface
 * snapshots envelope_eligible_at_decision=False and counts_as_envelope_error=False.
 * These are operational negative labels, useful on their own; they are not, and cannot
 * become, the autonomy corpus. Only bucket=matches rows (255,079 of them) can be that,
 * and they render on the classic results table alone.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { ResolutionGroupItems } from "@/components/reconciliation/resolution-group-items";
import {
  ResolutionGroupsTable,
  NeedsHumanWorksheet,
} from "@/components/reconciliation/resolution-groups-table";
import type { ReconResolutionGroup, ReconResolutionProposal } from "@/lib/types";

const rejectMutate = vi.fn();
vi.mock("@/hooks/use-reconciliation", () => ({
  useApproveResult: () => ({ mutate: vi.fn(), isPending: false }),
  useRejectResult: () => ({ mutate: rejectMutate, isPending: false }),
}));

function makeProposal(over: Partial<ReconResolutionProposal> = {}): ReconResolutionProposal {
  return {
    id: "p1",
    run_id: "r1",
    result_id: "res1",
    root_cause: "fees",
    action: "book_fee_line",
    booking_vehicle: "deposit",
    group_key: "fees:book_fee_line:deposit",
    source: "planner",
    narrative: "Stripe processing fee — book as a fee line.",
    proposed_amount: "3.20",
    currency: "USD",
    above_materiality: false,
    status: "proposed",
    failure_reason: null,
    correlation_id: null,
    created_at: "2026-07-06T00:00:00Z",
    order_reference: "R946866359",
    stripe_charge_id: "ch_3Nxxx",
    netsuite_internal_id: "12345",
    netsuite_record_type: "custdep",
    stripe_amount: "3.20",
    netsuite_amount: "3.15",
    variance_amount: "0.05",
    deposit_transaction_currency: null,
    deposit_foreign_amount: null,
    deposit_exchange_rate: null,
    ...over,
  } as ReconResolutionProposal;
}

let groupProposals: ReconResolutionProposal[] = [];
vi.mock("@/hooks/use-resolution", () => ({
  useGroupProposals: () => ({ data: groupProposals, isLoading: false }),
  NEEDS_HUMAN_PROPOSALS_LIMIT: 1000,
}));

const itemsProps = {
  runId: "r1",
  groupKey: "fees:book_fee_line:deposit",
  currency: "USD",
  tickedAboveIds: [] as string[],
  onTickAbove: vi.fn(),
  onInvestigate: vi.fn(),
};

beforeEach(() => {
  rejectMutate.mockReset();
  groupProposals = [makeProposal()];
  Object.assign(navigator, { clipboard: { writeText: vi.fn() } });
});

/** Drive the picker from an already-open control to a submitted reject. */
function pickAndSubmit(reasonPattern: RegExp) {
  fireEvent.click(screen.getByRole("radio", { name: reasonPattern }));
  fireEvent.click(screen.getByRole("button", { name: /^reject$/i }));
}

describe("reject on the expanded group's item list", () => {
  it("offers a reject control on an actionable item row", () => {
    render(<ResolutionGroupItems {...itemsProps} />);
    expect(screen.getByRole("button", { name: /reject match/i })).toBeInTheDocument();
  });

  it("shows short visible text but keeps the precise accessible name", () => {
    // Load-bearing for LAYOUT, not just wording. The long "Reject match" label pushed
    // this row's two buttons past the actions column, and under table-fixed the
    // overflow paints over the neighbouring narrative rather than pushing it —
    // reproduced in Chromium at 1440/1280/1152. Screen readers and getByRole still get
    // "Reject match" from aria-label, so shortening the visible text costs nothing.
    render(<ResolutionGroupItems {...itemsProps} />);
    const btn = screen.getByRole("button", { name: /^reject match$/i });
    expect(btn).toHaveTextContent(/^Reject$/);
  });

  it("submits the item's result_id, never the proposal id", () => {
    // The load-bearing assertion. PATCH /results/{id}/reject keys on the
    // ReconciliationResult; posting the proposal's own id would 404 at best and
    // label an unrelated row at worst. The two ids sit side by side on the object.
    render(<ResolutionGroupItems {...itemsProps} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    pickAndSubmit(/not the same money/i);

    expect(rejectMutate).toHaveBeenCalledWith(
      expect.objectContaining({ result_id: "res1", reason: "wrong_match" })
    );
    expect(rejectMutate).not.toHaveBeenCalledWith(expect.objectContaining({ result_id: "p1" }));
  });

  it("offers no reject control once the proposal has been approved", () => {
    // Group approve flips the underlying result to 'approved', which is terminal —
    // the API refuses the reject, so offering the control teaches the operator the
    // UI lies. The proposal's own status is the only signal this surface carries.
    groupProposals = [makeProposal({ status: "approved" })];
    render(<ResolutionGroupItems {...itemsProps} />);
    expect(screen.queryByRole("button", { name: /reject match/i })).not.toBeInTheDocument();
  });

  it("offers no reject control on a closed run", () => {
    // A closed period is a hard freeze; reject_result raises on it server-side.
    render(<ResolutionGroupItems {...itemsProps} disabled />);
    expect(screen.queryByRole("button", { name: /reject match/i })).not.toBeInTheDocument();
  });

  it("carries the same reason vocabulary as the classic table", () => {
    // One vocabulary, one component. Two divergent reason lists would split the
    // corpus into two incomparable label sets.
    render(<ResolutionGroupItems {...itemsProps} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    for (const label of [
      /not the same money/i,
      /amounts don't reconcile/i,
      /already applied elsewhere/i,
      /match is right/i,
      /something else/i,
    ]) {
      expect(screen.getByRole("radio", { name: label })).toBeInTheDocument();
    }
  });

  it("never reveals which reasons count against the matcher", () => {
    const { container } = render(<ResolutionGroupItems {...itemsProps} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/false.positive/i);
    expect(text).not.toMatch(/envelope.error/i);
    expect(text).not.toMatch(/error rate/i);
    expect(text).not.toMatch(/counts against/i);
  });
});

describe("reject on the needs-human worksheet", () => {
  const nhProposal = makeProposal({
    id: "p3",
    result_id: "res3",
    action: "needs_human",
    root_cause: "amount_mismatch",
    booking_vehicle: "none",
    narrative: "Ambiguous match — several open deposits share this amount and date.",
  });

  it("offers a reject control alongside Investigate in chat", () => {
    render(
      <NeedsHumanWorksheet runId="r1" proposals={[nhProposal]} isLoading={false} onInvestigate={vi.fn()} />
    );
    const row = screen.getByText("R946866359").closest("tr")!;
    expect(within(row).getByRole("button", { name: /investigate in chat/i })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /reject match/i })).toBeInTheDocument();
  });

  it("submits the item's result_id", () => {
    render(
      <NeedsHumanWorksheet runId="r1" proposals={[nhProposal]} isLoading={false} onInvestigate={vi.fn()} />
    );
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    pickAndSubmit(/already applied elsewhere/i);
    expect(rejectMutate).toHaveBeenCalledWith(
      expect.objectContaining({ result_id: "res3", reason: "duplicate" })
    );
  });

  it("offers no reject control on a closed run", () => {
    render(
      <NeedsHumanWorksheet
        runId="r1"
        proposals={[nhProposal]}
        isLoading={false}
        onInvestigate={vi.fn()}
        disabled
      />
    );
    expect(screen.queryByRole("button", { name: /reject match/i })).not.toBeInTheDocument();
  });
});

describe("the two rejects must not be confusable", () => {
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

  function groupsProps(over = {}) {
    return {
      runId: "r1",
      groups: [feeGroup],
      expandedKey: "fees:book_fee_line:deposit:USD",
      onToggleExpand: vi.fn(),
      isApproving: false,
      tickedAboveByGroup: {},
      onTickAbove: vi.fn(),
      groupResetSignals: {},
      onApprove: vi.fn(),
      onReject: vi.fn(),
      onInvestigate: vi.fn(),
      ...over,
    };
  }

  it("the group-level control says what it actually does — discard the plan, not reject a match", () => {
    // These mean OPPOSITE things and sat inches apart under the same word.
    // POST .../resolution-groups/{key}/reject is documented "Results are untouched";
    // PATCH /results/{id}/reject writes the negative label. A reviewer who means "this
    // match is wrong" and clicks the group button gets NO label and no error — which is
    // one plausible reason the corpus is empty after the feature shipped.
    const onReject = vi.fn();
    render(<ResolutionGroupsTable {...groupsProps({ onReject })} />);

    const discard = screen.getByRole("button", { name: /discard plan/i });
    fireEvent.click(discard);
    expect(onReject).toHaveBeenCalledWith(feeGroup);
  });

  it("no control on the groups worksheet is labelled the bare word 'Reject'", () => {
    render(<ResolutionGroupsTable {...groupsProps()} />);
    expect(screen.queryByRole("button", { name: /^reject$/i })).not.toBeInTheDocument();
  });

  it("discarding the plan never touches a result", () => {
    render(<ResolutionGroupsTable {...groupsProps()} />);
    fireEvent.click(screen.getByRole("button", { name: /discard plan/i }));
    expect(rejectMutate).not.toHaveBeenCalled();
  });
});
