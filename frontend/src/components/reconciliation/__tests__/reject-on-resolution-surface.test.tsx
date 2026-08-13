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
  useRejectResult: () => ({ mutate: rejectMutate, isPending: false, reset: vi.fn() }),
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
    // The RESULT's own disposition — what the reject endpoint actually enforces.
    result_status: "pending",
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
      expect.objectContaining({ result_id: "res1", reason: "wrong_match" }),
      expect.anything()
    );
    expect(rejectMutate).not.toHaveBeenCalledWith(expect.objectContaining({ result_id: "p1" }));
  });

  it("scrolling INSIDE the picker does not destroy the operator's input", () => {
    // The close-on-scroll listener is capture-phase on window, so it also fired for
    // scrolls originating inside the dialog's own overflow box and the note textarea.
    // Typing a multi-line note — mandatory for reason 'other' — scrolled the textarea
    // and deleted the whole picker mid-sentence, with no error. It also nullified the
    // maxHeight clamp, whose entire purpose is to let a tall picker be scrolled.
    render(<ResolutionGroupItems {...itemsProps} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    fireEvent.click(screen.getByRole("radio", { name: /something else/i }));
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "a long explanation that wraps" } });

    fireEvent.scroll(textarea);
    expect(screen.queryByRole("dialog", { name: /reject this match/i })).toBeInTheDocument();
    fireEvent.scroll(screen.getByRole("dialog", { name: /reject this match/i }));
    expect(screen.queryByRole("dialog", { name: /reject this match/i })).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("a long explanation that wraps");
  });

  it("a refused reject stays open, shows why, and keeps the typed note", () => {
    // submit() used to close unconditionally, discarding both the error and the note.
    // A failed reject then looked exactly like a successful one, so the operator
    // repeats it — and on a feature whose only product is the label, a silently
    // dropped label is the worst available outcome.
    rejectMutate.mockImplementation((_vars, opts) => {
      void opts; // onSuccess deliberately NOT invoked: this is the refusal path
    });
    render(<ResolutionGroupItems {...itemsProps} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    fireEvent.click(screen.getByRole("radio", { name: /not the same money/i }));
    fireEvent.click(screen.getByRole("button", { name: /^reject$/i }));

    expect(screen.queryByRole("dialog", { name: /reject this match/i })).toBeInTheDocument();
  });

  it("offers no reject control once the underlying RESULT is terminal", () => {
    // The gate that matters. reject_result mutates the result and never the proposal,
    // so gating on the proposal's status could not observe a reject at all: the row
    // kept its Reject button forever, even across a reload.
    groupProposals = [makeProposal({ status: "proposed", result_status: "rejected" })];
    render(<ResolutionGroupItems {...itemsProps} />);
    expect(screen.queryByRole("button", { name: /reject match/i })).not.toBeInTheDocument();
    const cells = screen.getByText("R946866359").closest("tr")!.querySelectorAll("td");
    expect(within(cells[cells.length - 1] as HTMLElement).getByText(/^Rejected$/)).toBeInTheDocument();
  });

  it("un-ticks an above-materiality row when its match is rejected", () => {
    // Tick-then-reject on the same row. The checkbox is gated on the PROPOSAL's status,
    // which a reject never changes, so it stayed visible and checked on a row that can
    // no longer be approved — and the parent's ticked set still held the id, so
    // oneClickCount = proposed_count - above_materiality_count + includedAboveIds.length
    // recomputed to the SAME number after the server-side counts dropped. "Approve N"
    // overstated what would succeed, permanently. The server refuses the terminal row,
    // so nothing is double-processed; the number was simply a lie.
    const onTickAbove = vi.fn();
    groupProposals = [makeProposal({ above_materiality: true })];
    rejectMutate.mockImplementation((_vars, opts) => opts?.onSuccess?.());

    render(<ResolutionGroupItems {...itemsProps} tickedAboveIds={["p1"]} onTickAbove={onTickAbove} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    pickAndSubmit(/not the same money/i);

    expect(onTickAbove).toHaveBeenCalledWith("p1", false);
  });

  it("hides the approval checkbox once the row's result is terminal", () => {
    groupProposals = [makeProposal({ above_materiality: true, result_status: "rejected" })];
    render(<ResolutionGroupItems {...itemsProps} />);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("offers no reject control once a group approve has decided the row", () => {
    // Group approve flips the underlying RESULT to 'approved', which is terminal, so
    // the API refuses the reject. The signal is result_status — the proposal also
    // going 'approved' here is incidental, and relying on it was the original bug.
    groupProposals = [makeProposal({ status: "approved", result_status: "approved" })];
    render(<ResolutionGroupItems {...itemsProps} />);
    expect(screen.queryByRole("button", { name: /reject match/i })).not.toBeInTheDocument();
    // Scoped to the ACTIONS cell: "Approved" also renders as the proposal status chip
    // in this same row, so an unscoped query matches two nodes and would pass even if
    // the actions cell rendered nothing at all.
    const cells = screen.getByText("R946866359").closest("tr")!.querySelectorAll("td");
    expect(within(cells[cells.length - 1] as HTMLElement).getByText(/^Approved$/)).toBeInTheDocument();
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
    // Reads the DIALOG, not render()'s `container`. The picker is portalled to
    // document.body, so `container.textContent` never contained a single word of it —
    // this guard passed against a build with "false positive / envelope error /
    // counts against" injected verbatim into the picker (21/21 green). It shipped
    // vacuous in #193 and was copied forward here unchecked. The invariant it protects
    // is real: 3 of 5 reasons feed the error metric, and a reviewer who can see the
    // weighting can pick the reason that produces the number they want.
    render(<ResolutionGroupItems {...itemsProps} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    const dialog = screen.getByRole("dialog", { name: /reject this match/i });
    const text = dialog.textContent ?? "";
    expect(text).toMatch(/not the same money/i); // anchor: we are reading the real picker
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
      expect.objectContaining({ result_id: "res3", reason: "duplicate" }),
      expect.anything()
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
