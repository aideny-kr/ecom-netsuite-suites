import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReconExceptionCard } from "@/components/reconciliation/recon-exception-card";
import type { ReconResult } from "@/lib/types";

const exceptionResult: ReconResult = {
  id: "result-exc-001",
  run_id: "run-001",
  payout_id: "po_exc_001",
  deposit_id: "dep_exc_001",
  match_type: "deterministic",
  confidence: 0.95,
  status: "suggested",
  stripe_amount: 200.0,
  netsuite_amount: 195.0,
  variance_amount: 5.0,
  variance_type: "amount_mismatch",
  variance_explanation: "Small rounding discrepancy",
  currency: "USD",
  match_rule: null,
  evidence: {
    order_reference: "R987654321",
  },
  approved_by: null,
  approved_at: null,
  created_at: "2026-06-01T00:00:00Z",
};

const unmatchedResult: ReconResult = {
  id: "result-unm-001",
  run_id: "run-001",
  payout_id: "po_unm_001",
  deposit_id: null,
  match_type: "unmatched",
  confidence: 0.0,
  status: "pending",
  stripe_amount: 75.0,
  netsuite_amount: null,
  variance_amount: 75.0,
  variance_type: null,
  variance_explanation: null,
  currency: "USD",
  match_rule: null,
  evidence: null,
  approved_by: null,
  approved_at: null,
  created_at: "2026-06-01T00:00:00Z",
};

describe("ReconExceptionCard — confidence is advisory, not a match verdict", () => {
  it("renders the confidence badge with the word 'confidence' (not 'match')", () => {
    render(<ReconExceptionCard result={exceptionResult} />);
    expect(screen.getByText(/95% confidence/i)).toBeInTheDocument();
  });

  it("badge text does NOT contain '% match'", () => {
    render(<ReconExceptionCard result={exceptionResult} />);
    // Make sure the old "% match" label is gone
    expect(screen.queryByText(/% match/i)).not.toBeInTheDocument();
  });

  it("confidence badge has an advisory tooltip", () => {
    render(<ReconExceptionCard result={exceptionResult} />);
    const badge = screen.getByText(/95% confidence/i);
    expect(badge).toHaveAttribute("title");
    expect(badge.getAttribute("title")).toMatch(/advisory/i);
  });

  it("confidence badge is neutral (muted), NOT verdict-colored", () => {
    render(<ReconExceptionCard result={exceptionResult} />);
    const badge = screen.getByText(/95% confidence/i);
    // Mirror the table's regression: the confidence number must not be color-coded
    expect(badge).toHaveClass("text-muted-foreground");
    expect(badge).not.toHaveClass("text-orange-600");
  });

  it("does not render a confidence badge for unmatched results", () => {
    render(<ReconExceptionCard result={unmatchedResult} />);
    // The badge is only shown for !isUnmatched
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("renders exception label and order reference", () => {
    render(<ReconExceptionCard result={exceptionResult} />);
    expect(screen.getByText("amount_mismatch")).toBeInTheDocument();
    expect(screen.getByText("R987654321")).toBeInTheDocument();
  });
});
