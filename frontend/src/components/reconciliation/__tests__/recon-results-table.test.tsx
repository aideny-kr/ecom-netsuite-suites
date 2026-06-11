import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReconResultsTable } from "@/components/reconciliation/recon-results-table";
import type { ReconResult } from "@/lib/types";

// Mock the hook that requires a React Query context
vi.mock("@/hooks/use-reconciliation", () => ({
  useApproveResult: () => ({ mutate: vi.fn(), isPending: false }),
}));

const autoMatchedResult: ReconResult = {
  id: "result-001",
  run_id: "run-001",
  payout_id: "po_001",
  deposit_id: "dep_001",
  match_type: "deterministic",
  confidence: 0.60,
  status: "auto_matched",
  stripe_amount: 100.0,
  netsuite_amount: 100.0,
  variance_amount: 0,
  variance_type: null,
  variance_explanation: null,
  currency: "USD",
  match_rule: null,
  evidence: {
    order_reference: "R123456789",
  },
  approved_by: null,
  approved_at: null,
  created_at: "2026-06-01T00:00:00Z",
};

const pendingLowConfidenceResult: ReconResult = {
  id: "result-002",
  run_id: "run-001",
  payout_id: "po_002",
  deposit_id: null,
  match_type: "unmatched",
  confidence: 0.45,
  status: "pending",
  stripe_amount: 50.0,
  netsuite_amount: null,
  variance_amount: 50.0,
  variance_type: "amount_mismatch",
  variance_explanation: "No matching deposit found",
  currency: "USD",
  match_rule: null,
  evidence: null,
  approved_by: null,
  approved_at: null,
  created_at: "2026-06-01T00:00:00Z",
};

describe("ReconResultsTable — confidence is advisory, not a verdict", () => {
  it("labels the column 'Advisory Score', never 'Confidence'", () => {
    render(<ReconResultsTable results={[autoMatchedResult]} />);
    // The persisted value is the R2 advisory composite — a bare "Confidence"
    // header reads as an engine verdict (the Status badge is the disposition).
    expect(screen.getByText("Advisory Score")).toBeInTheDocument();
    expect(screen.queryByText("Confidence")).not.toBeInTheDocument();
  });

  it("renders the confidence value as a percentage", () => {
    render(<ReconResultsTable results={[autoMatchedResult]} />);
    expect(screen.getByText("60%")).toBeInTheDocument();
  });

  it("confidence cell carries text-muted-foreground (neutral) for a low-confidence auto_matched row", () => {
    render(<ReconResultsTable results={[autoMatchedResult]} />);
    const confidenceSpan = screen.getByText("60%");
    expect(confidenceSpan).toHaveClass("text-muted-foreground");
  });

  it("confidence cell does NOT carry any red verdict class for a low-confidence auto_matched row", () => {
    render(<ReconResultsTable results={[autoMatchedResult]} />);
    const confidenceSpan = screen.getByText("60%");
    expect(confidenceSpan).not.toHaveClass("text-red-600");
  });

  it("confidence cell does NOT carry orange or green verdict classes", () => {
    render(<ReconResultsTable results={[autoMatchedResult]} />);
    const confidenceSpan = screen.getByText("60%");
    expect(confidenceSpan).not.toHaveClass("text-orange-600");
    expect(confidenceSpan).not.toHaveClass("text-green-600");
  });

  it("confidence span has an advisory tooltip explaining it is not a status verdict", () => {
    render(<ReconResultsTable results={[autoMatchedResult]} />);
    const confidenceSpan = screen.getByText("60%");
    expect(confidenceSpan).toHaveAttribute("title");
    // Must mention it's advisory — not a verdict
    expect(confidenceSpan.getAttribute("title")).toMatch(/advisory/i);
  });

  it("Status badge for auto_matched shows the green class (authoritative disposition)", () => {
    render(<ReconResultsTable results={[autoMatchedResult]} />);
    const badge = screen.getByText("auto_matched");
    expect(badge).toHaveClass("bg-green-100");
    expect(badge).toHaveClass("text-green-800");
  });

  it("renders multiple rows without mixing up confidence colors", () => {
    render(<ReconResultsTable results={[autoMatchedResult, pendingLowConfidenceResult]} />);
    // Both confidence cells should be neutral, not verdict-colored
    const cells = screen.getAllByText(/^\d+%$/);
    for (const cell of cells) {
      expect(cell).not.toHaveClass("text-red-600");
      expect(cell).not.toHaveClass("text-orange-600");
      expect(cell).not.toHaveClass("text-green-600");
    }
  });

  it("renders the order_reference from evidence", () => {
    render(<ReconResultsTable results={[autoMatchedResult]} />);
    expect(screen.getByText("R123456789")).toBeInTheDocument();
  });
});
