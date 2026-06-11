import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

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
let mockRun: { id: string; status: string; total_variance: number; date_from?: string } = {
  id: "r1",
  status: "completed",
  total_variance: -42.5,
  date_from: "2026-05-01",
};
let mockApproveData: { approved_count: number; skipped_count: number } | undefined;

// CloseChecklist is mocked to expose the props it receives so we can assert
// page.tsx hands it the server-computed bucket summary (close_readiness counts
// over the FULL run), and no longer a paged results array.
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
      close_readiness: { open_exceptions: 0, suggested: 0, left_for_review: 0 },
    },
  }),
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
vi.mock("@/hooks/use-features", () => ({ useFeature: () => true }));
vi.mock("@/components/reconciliation/data-freshness-banner", () => ({
  DataFreshnessBanner: () => null,
}));
vi.mock("@/components/reconciliation/close-checklist", () => ({
  CloseChecklist: (props: { summary?: unknown; results?: unknown }) => {
    closeChecklistSpy(props);
    return null;
  },
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import ReconciliationPage from "@/app/(dashboard)/reconciliation/page";

beforeEach(() => {
  mutate.mockReset();
  closeChecklistSpy.mockReset();
  mockRun = {
    id: "r1",
    status: "completed",
    total_variance: -42.5,
    date_from: "2026-05-01",
  };
  mockApproveData = undefined;
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

  it("passes the server-computed bucket summary to the CloseChecklist", () => {
    render(<ReconciliationPage />);
    // CloseChecklist gates period close on summary.close_readiness — live SQL
    // counts over the FULL run — and no longer receives a (paged) results array.
    expect(closeChecklistSpy).toHaveBeenCalled();
    const lastCall = closeChecklistSpy.mock.calls.at(-1)?.[0];
    expect(lastCall.summary).toMatchObject({
      run_id: "r1",
      close_readiness: { open_exceptions: 0, suggested: 0, left_for_review: 0 },
    });
    expect(lastCall.results).toBeUndefined();
  });

  it("surfaces approved/skipped counts after a successful bulk approve", () => {
    mockApproveData = { approved_count: 7000, skipped_count: 335 };
    render(<ReconciliationPage />);
    expect(screen.getByText(/Approved 7000/)).toBeInTheDocument();
    expect(screen.getByText(/skipped 335/)).toBeInTheDocument();
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
