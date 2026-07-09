import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ResolutionSummaryHeader } from "@/components/reconciliation/resolution-summary-header";
import type { ReconResolutionSummary } from "@/lib/types";

const summary: ReconResolutionSummary = {
  run_id: "r1",
  total_results: 1000,
  matches_count: 900,
  match_rate: "90.0",
  proposals_count: 100,
  explained_count: 75,
  explained_rate: "75.0",
  guard_skipped_count: 0,
  variance_by_root_cause: { fees: "1284.55", timing: "0.00" },
  groups: [],
};

describe("ResolutionSummaryHeader", () => {
  it("renders match rate, explained rate, and root-cause breakdown", () => {
    render(<ResolutionSummaryHeader summary={summary} />);
    expect(screen.getByText(/90\.0%/)).toBeInTheDocument();
    expect(screen.getByText(/75\.0%/)).toBeInTheDocument();
    expect(screen.getByText(/fees/i)).toBeInTheDocument();
    // Amount appears twice: in Gross exception card + root-cause breakdown
    expect(screen.getAllByText(/\$1,284\.55/)).toHaveLength(2);
  });

  it("renders the empty state when summary is null", () => {
    render(<ResolutionSummaryHeader summary={null} />);
    expect(screen.getByText(/no reconciliation run selected/i)).toBeInTheDocument();
  });
});
