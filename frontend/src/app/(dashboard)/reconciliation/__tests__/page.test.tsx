import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const mutate = vi.fn();
vi.mock("@/hooks/use-reconciliation", () => ({
  useReconRuns: () => ({ data: [{ id: "r1", status: "completed" }] }),
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
  useApproveBucket: () => ({ mutate, isPending: false }),
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
  CloseChecklist: () => null,
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import ReconciliationPage from "@/app/(dashboard)/reconciliation/page";

beforeEach(() => mutate.mockReset());

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
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ bucket: expect.any(String) }),
    );
  });
});
