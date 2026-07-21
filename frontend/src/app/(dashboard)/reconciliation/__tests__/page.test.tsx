import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { ReconResolutionProposal, ReconResolutionGroup } from "@/lib/types";

// mutate mirrors React Query's signature: (vars, options?) — the page passes a
// per-call onSuccess to bump the reset-signal, so the mock must invoke it to
// exercise the "note clears after a successful approve" path.
const mutate = vi.fn(
  (
    _vars: unknown,
    options?: { onSuccess?: () => void },
  ) => {
    options?.onSuccess?.();
  },
);

// Mutable run + approve-result state so individual tests can drive the
// run status (completed vs closed) and the mutation success payload.
let mockRun: { id: string; status: string; total_variance: number; date_from?: string; date_to?: string } = {
  id: "r1",
  status: "completed",
  total_variance: -42.5,
  date_from: "2026-05-01",
};
let mockApproveData: { approved_count: number; skipped_count: number } | undefined;

// CloseChecklist is mocked to expose the props it receives so we can assert
// page.tsx hands it the run + period only — readiness is PERIOD-scoped and the
// component fetches it itself via useCloseReadiness (R3-A); it no longer
// receives a per-run bucket summary or a paged results array.
const closeChecklistSpy = vi.fn();

vi.mock("@/hooks/use-reconciliation", () => ({
  useReconRuns: () => ({ data: [mockRun] }),
  useReconResults: () => ({ data: [], isLoading: false }),
  useReconBucketSummary: () => ({
    data: {
      run_id: "r1",
      matches: { count: 7335, total_variance: 0 },
      rules: { count: 54, total_variance: 12 },
      auto_classifications: { count: 1072, total_variance: 9.24 },
      needs_review: { count: 869, total_variance: 1203 },
    },
  }),
  useCloseReadiness: () => ({ data: undefined, isLoading: true }),
  useApproveBucket: () => ({ mutate, isPending: false, data: mockApproveData }),
  useApproveResult: () => ({ mutate: vi.fn() }),
  useClosePeriod: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/use-recon-pipeline", () => ({
  useReconPipeline: () => ({
    runPipeline: vi.fn(),
    runId: null,
    isRunning: false,
    stages: [],
    progress: 0,
    error: null,
    summary: null,
  }),
}));
// This suite mostly exercises the classic (flag-off) surface —
// resolutionUiFlag defaults false so page.tsx renders the untouched
// tabs+table+bulk-card block directly, matching most of these tests'
// assertions. Other flags (e.g. "reconciliation") keep the prior
// blanket-true behavior. One test below flips resolutionUiFlag to true to
// cover the flag-ON surface.
let resolutionUiFlag = false;
vi.mock("@/hooks/use-features", () => ({
  useFeature: (key: string) => (key === "recon_resolution_ui" ? resolutionUiFlag : true),
}));
// Mutable — the investigate-prefill suite below drives a group's proposals
// through the real ResolutionGroupItems component (rather than mocking it
// out) so handleInvestigateProposal gets exercised end-to-end via a real
// button click.
let mockResolutionSummaryData: Record<string, unknown> | undefined;
let mockGroupProposals: ReconResolutionProposal[] = [];
// Needs-human items are fetched cross-group (action=needs_human) for the
// "Needs human review" worksheet, separate from the per-group fetch above.
let mockNeedsHumanProposals: ReconResolutionProposal[] = [];
// page.tsx now unconditionally calls the resolution hooks (Rules of Hooks) —
// mock them like use-reconciliation above so no real QueryClientProvider is
// needed for this classic-view suite.
vi.mock("@/hooks/use-resolution", () => ({
  NEEDS_HUMAN_PROPOSALS_LIMIT: 1000,
  useResolutionSummary: () => ({ data: mockResolutionSummaryData, isLoading: false }),
  useApproveResolutionGroup: () => ({ mutate: vi.fn(), isPending: false }),
  useRejectResolutionGroup: () => ({ mutate: vi.fn(), isPending: false }),
  useGroupProposals: () => ({ data: mockGroupProposals, isLoading: false }),
  useNeedsHumanProposals: () => ({ data: mockNeedsHumanProposals, isLoading: false }),
}));
vi.mock("@/components/reconciliation/data-freshness-banner", () => ({
  DataFreshnessBanner: () => null,
}));
vi.mock("@/components/reconciliation/close-checklist", () => ({
  CloseChecklist: (props: { summary?: unknown; results?: unknown }) => {
    closeChecklistSpy(props);
    return null;
  },
}));
// A stable fn (not a fresh vi.fn() per render) so tests can assert on calls
// across a render + interaction.
const routerPush = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: routerPush }) }));

import ReconciliationPage from "@/app/(dashboard)/reconciliation/page";

beforeEach(() => {
  mutate.mockReset();
  closeChecklistSpy.mockReset();
  routerPush.mockReset();
  resolutionUiFlag = false;
  mockRun = {
    id: "r1",
    status: "completed",
    total_variance: -42.5,
    date_from: "2026-05-01",
  };
  mockApproveData = undefined;
  mockResolutionSummaryData = undefined;
  mockGroupProposals = [];
  mockNeedsHumanProposals = [];
});

describe("ReconciliationPage four buckets", () => {
  it("renders the four bucket tabs with counts from the summary", () => {
    render(<ReconciliationPage />);
    // "Matches" / "Needs Review" appear in both the summary bar and the tabs,
    // so assert at least one of each is present.
    expect(screen.getAllByText(/Matches/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/7335|7,335/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Needs.?Review/i).length).toBeGreaterThan(0);
  });

  it("bulk-approves a bulk-approvable bucket", async () => {
    render(<ReconciliationPage />);
    fireEvent.click(screen.getByRole("button", { name: /approve all/i }));
    await waitFor(() => expect(mutate).toHaveBeenCalled());
    // Second arg is the per-call React Query options ({ onSuccess }).
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ bucket: expect.any(String) }),
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it("threads bulk-approval notes into the mutation payload", async () => {
    render(<ReconciliationPage />);
    const notes = screen.getByPlaceholderText(/note/i);
    fireEvent.change(notes, { target: { value: "Q2 close" } });
    fireEvent.click(screen.getByRole("button", { name: /approve all/i }));
    await waitFor(() => expect(mutate).toHaveBeenCalled());
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ bucket: expect.any(String), notes: "Q2 close" }),
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });

  it("shows the signed-net Total Variance from the run, not the bucket sum", () => {
    // Bucket sum would be 0 + 12 + 9.24 + 1203 = 1224.24 (sum of per-bucket
    // gross-abs totals). run.total_variance is the run-level SIGNED-NET figure
    // (matches the evidence pack; can be negative on refund-heavy periods) —
    // that is what must render, not the re-summed per-bucket gross-abs totals.
    render(<ReconciliationPage />);
    // Exact-case label on the summary-bar card (the bulk-approval card uses
    // lowercase "total variance", so match the capitalized summary label).
    expect(screen.getByText("Total Variance")).toBeInTheDocument();
    expect(screen.getByText(/-\$42\.50/)).toBeInTheDocument();
    expect(screen.queryByText(/1,?224\.24/)).not.toBeInTheDocument();
  });

  it("does NOT render the bulk-approval card on a closed run", () => {
    mockRun = {
      id: "r1",
      status: "closed",
      total_variance: -42.5,
      date_from: "2026-05-01",
    };
    render(<ReconciliationPage />);
    expect(
      screen.queryByRole("button", { name: /approve all/i }),
    ).not.toBeInTheDocument();
  });

  it("passes only run + period to the CloseChecklist (readiness is period-scoped)", () => {
    render(<ReconciliationPage />);
    // R3-A: the checklist gates period close on GET /close-readiness/{period}
    // (fetched inside the component via useCloseReadiness) — page.tsx no longer
    // hands it a per-run bucket summary or a (paged) results array.
    expect(closeChecklistSpy).toHaveBeenCalled();
    const lastCall = closeChecklistSpy.mock.calls.at(-1)?.[0];
    expect(lastCall.run).toMatchObject({ id: "r1" });
    expect(lastCall.period).toBe("2026-05");
    expect(lastCall.summary).toBeUndefined();
    expect(lastCall.results).toBeUndefined();
  });

  it("surfaces approved/skipped counts after a successful bulk approve", () => {
    mockApproveData = { approved_count: 7000, skipped_count: 335 };
    render(<ReconciliationPage />);
    expect(screen.getByText(/Approved 7000/)).toBeInTheDocument();
    expect(screen.getByText(/skipped 335/)).toBeInTheDocument();
  });

  it("renders an Export menu beside the classic bucket results table (section=results)", () => {
    render(<ReconciliationPage />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    const [csv, xlsx] = screen.getAllByRole("menuitem");
    expect(csv).toHaveAttribute(
      "href",
      "/api/v1/reconciliation/runs/r1/export?section=results&format=csv",
    );
    // "Full result set" — the export's column set differs from what's on
    // screen in classic view (the evidence "All Results" columns), unlike
    // the default "visible columns" label.
    expect(csv).toHaveTextContent("CSV — full result set");
    expect(xlsx).toHaveTextContent("Excel — full result set");
  });

  it("does NOT carry a typed note across buckets", () => {
    render(<ReconciliationPage />);
    // Type a note on the Matches bucket.
    const notes = screen.getByPlaceholderText(/note/i) as HTMLInputElement;
    fireEvent.change(notes, { target: { value: "for matches only" } });
    expect(notes.value).toBe("for matches only");
    // Switch to the Rules bucket (also bulk-approvable). The card is keyed by
    // active bucket, so it remounts with a fresh, empty note — the audit note
    // for one bucket must never ride into another bucket's approve.
    fireEvent.click(screen.getByRole("button", { name: /^Rules \(/i }));
    expect(
      (screen.getByPlaceholderText(/note/i) as HTMLInputElement).value,
    ).toBe("");
  });

  it("clears the note after a successful bulk approve", async () => {
    render(<ReconciliationPage />);
    const notes = screen.getByPlaceholderText(/note/i) as HTMLInputElement;
    fireEvent.change(notes, { target: { value: "Q2 close" } });
    fireEvent.click(screen.getByRole("button", { name: /approve all/i }));
    // mutate's mock invokes onSuccess → page bumps the reset-signal → card
    // clears its note so it can't ride into a re-approval.
    await waitFor(() =>
      expect(
        (screen.getByPlaceholderText(/note/i) as HTMLInputElement).value,
      ).toBe(""),
    );
  });
});

describe("ReconciliationPage summary-first surface (flag ON)", () => {
  it("renders the CloseChecklist without opening classic view", () => {
    // T2 gate finding: CloseChecklist was rendered only inside
    // renderClassicBucketView(), which the flag-ON branch only calls when
    // "Show all results (classic view)" is toggled open — burying the
    // period-close entry point. It must render unconditionally instead.
    resolutionUiFlag = true;
    render(<ReconciliationPage />);
    expect(closeChecklistSpy).toHaveBeenCalled();
    expect(
      screen.queryByText(/Show all results \(classic view\)/i),
    ).toBeInTheDocument();
  });
});

describe("ReconciliationPage investigate-proposal prefill", () => {
  const baseProposal: ReconResolutionProposal = {
    id: "p1",
    run_id: "r1",
    result_id: "res1",
    root_cause: "missing",
    action: "needs_human",
    booking_vehicle: "none",
    group_key: "missing:needs_human:none",
    source: "planner",
    narrative: "No matching NetSuite deposit found.",
    proposed_amount: "42.00",
    currency: "USD",
    above_materiality: false,
    status: "proposed",
    failure_reason: null,
    correlation_id: null,
    created_at: "2026-07-06T00:00:00Z",
    order_reference: null,
    stripe_charge_id: "ch_abc",
    netsuite_internal_id: null,
    netsuite_record_type: null,
    stripe_amount: null,
    netsuite_amount: null,
    variance_amount: null,
  };

  const mockGroup: ReconResolutionGroup = {
    group_key: "missing:needs_human:none",
    root_cause: "missing",
    action: "needs_human",
    booking_vehicle: "none",
    currency: "USD",
    count: 1,
    proposed_count: 1,
    approved_count: 0,
    total_amount: "42.00",
    above_materiality_count: 0,
  };

  const summaryBase = {
    run_id: "r1",
    total_results: 1,
    matches_count: 0,
    match_rate: "0",
    proposals_count: 1,
    explained_count: 0,
    explained_rate: "0",
    guard_skipped_count: 0,
    variance_by_root_cause: { missing: "42.00" },
    groups: [mockGroup],
    agent_job: null,
  };

  beforeEach(() => {
    resolutionUiFlag = true;
    mockRun.date_to = "2026-05-31";
  });

  // needs_human proposals render directly (as flat rows) in the "Needs human
  // review" worksheet — no group-card expand step needed — then clicking
  // "Investigate in chat" exercises handleInvestigateProposal end to end via
  // a real DOM interaction. The SQL date filter is driven by the page's own
  // date-range pickers (not selectedRun's dates), so setDates fills those in
  // via fireEvent.change.
  function renderAndInvestigate(proposal: ReconResolutionProposal, opts: { setDates?: boolean } = {}) {
    mockNeedsHumanProposals = [proposal];
    mockResolutionSummaryData = summaryBase;
    const { container } = render(<ReconciliationPage />);
    if (opts.setDates) {
      const dateInputs = container.querySelectorAll('input[type="date"]');
      fireEvent.change(dateInputs[0], { target: { value: "2026-05-01" } });
      fireEvent.change(dateInputs[1], { target: { value: "2026-05-31" } });
    }
    fireEvent.click(screen.getByText(/investigate in chat/i));
  }

  it("uses an exact t.id lookup when netsuite_internal_id is present, no LIKE or date filter", () => {
    renderAndInvestigate({
      ...baseProposal,
      netsuite_internal_id: "98765",
      netsuite_record_type: "custdep",
    });
    expect(routerPush).toHaveBeenCalledTimes(1);
    const query = decodeURIComponent(routerPush.mock.calls[0][0] as string);
    expect(query).toContain("t.id = 98765");
    expect(query).toContain("record type: custdep");
    expect(query).not.toContain("LIKE");
    expect(query).not.toContain("TO_DATE");
  });

  it("searches memo (not tranid) with the CustDep/Deposit type set and date range when only order_reference is present", () => {
    renderAndInvestigate(
      { ...baseProposal, order_reference: "R946866359" },
      { setDates: true },
    );
    expect(routerPush).toHaveBeenCalledTimes(1);
    const query = decodeURIComponent(routerPush.mock.calls[0][0] as string);
    expect(query).toContain("t.memo LIKE '%R946866359%'");
    expect(query).not.toContain("t.tranid LIKE");
    expect(query).toContain("t.type IN ('CustDep', 'Deposit')");
    expect(query).toContain("TO_DATE('2026-05-01'");
    expect(query).toContain("TO_DATE('2026-05-31'");
  });

  it("falls back to the amount+date search when neither identifier is present", () => {
    renderAndInvestigate({ ...baseProposal });
    expect(routerPush).toHaveBeenCalledTimes(1);
    const query = decodeURIComponent(routerPush.mock.calls[0][0] as string);
    expect(query).toContain("t.type = 'CustDep'");
    expect(query).toContain("BETWEEN");
    expect(query).not.toContain("t.memo LIKE");
    expect(query).not.toContain("t.id =");
  });
});
