import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ReconResultsTable } from "@/components/reconciliation/recon-results-table";
import type { ReconResult } from "@/lib/types";

const rejectMutate = vi.fn();
vi.mock("@/hooks/use-reconciliation", () => ({
  useApproveResult: () => ({ mutate: vi.fn(), isPending: false }),
  useRejectResult: () => ({ mutate: rejectMutate, isPending: false }),
}));

function makeResult(over: Partial<ReconResult> = {}): ReconResult {
  return {
    id: "res-1",
    run_id: "run-1",
    payout_id: "po_1",
    deposit_id: "dep_1",
    match_type: "deterministic",
    confidence: 1,
    status: "suggested",
    stripe_amount: 1284,
    netsuite_amount: 1284,
    variance_amount: 0,
    variance_type: null,
    variance_explanation: null,
    currency: "USD",
    match_rule: null,
    evidence: { order_reference: "FW-104882" },
    approved_by: null,
    approved_at: null,
    created_at: "2026-08-01T00:00:00Z",
    ...over,
  };
}

beforeEach(() => rejectMutate.mockReset());

describe("reject action", () => {
  it("offers reject on a row a human can still act on", () => {
    render(<ReconResultsTable results={[makeResult()]} />);
    expect(screen.getByRole("button", { name: /reject match/i })).toBeInTheDocument();
  });

  it("offers NO reject control on a terminal row — the API refuses it anyway", () => {
    // Showing a control the server will reject teaches the operator the UI lies.
    render(<ReconResultsTable results={[makeResult({ status: "approved" })]} />);
    expect(screen.queryByRole("button", { name: /reject match/i })).not.toBeInTheDocument();
  });

  it("does not submit until a reason is picked — reason is the whole point", () => {
    render(<ReconResultsTable results={[makeResult()]} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));

    const confirm = screen.getByRole("button", { name: /^reject$/i });
    expect(confirm).toBeDisabled();
    expect(rejectMutate).not.toHaveBeenCalled();
  });

  it("submits the picked reason", () => {
    render(<ReconResultsTable results={[makeResult()]} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    fireEvent.click(screen.getByRole("radio", { name: /not the same money/i }));
    fireEvent.click(screen.getByRole("button", { name: /^reject$/i }));

    expect(rejectMutate).toHaveBeenCalledWith(
      expect.objectContaining({ result_id: "res-1", reason: "wrong_match" })
    );
  });

  it("requires a note for 'something else' and blocks submit until it has one", () => {
    // Mirrors the service, which 400s on reason='other' with a blank note. Enforcing
    // it here means the round trip that would fail never happens.
    render(<ReconResultsTable results={[makeResult()]} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    fireEvent.click(screen.getByRole("radio", { name: /something else/i }));

    expect(screen.getByRole("button", { name: /^reject$/i })).toBeDisabled();

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "vendor said to hold" } });
    expect(screen.getByRole("button", { name: /^reject$/i })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /^reject$/i }));

    expect(rejectMutate).toHaveBeenCalledWith(
      expect.objectContaining({ reason: "other", note: "vendor said to hold" })
    );
  });

  it("never reveals which reasons count against the matcher", () => {
    // Deliberate: 3 of 5 reasons feed the false-positive rate. If a reviewer can see
    // the weighting they can pick the reason that produces the number they want, and
    // the corpus stops being evidence about the matcher.
    const { container } = render(<ReconResultsTable results={[makeResult()]} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));

    const text = container.textContent ?? "";
    // Post-rename a leak would read "envelope error", which matched none of the
    // original three patterns — the guard had quietly stopped covering the
    // vocabulary the product actually uses.
    expect(text).not.toMatch(/false.positive/i);
    expect(text).not.toMatch(/envelope.error/i);
    expect(text).not.toMatch(/error rate/i);
    expect(text).not.toMatch(/counts against/i);
  });

  it("shows the reason on an already-rejected row", () => {
    render(
      <ReconResultsTable
        results={[makeResult({ status: "rejected", reject_reason: "duplicate" })]}
      />
    );
    expect(screen.getByText(/already applied elsewhere/i)).toBeInTheDocument();
  });

  it("cancel closes the picker without submitting", () => {
    render(<ReconResultsTable results={[makeResult()]} />);
    fireEvent.click(screen.getByRole("button", { name: /reject match/i }));
    fireEvent.click(screen.getByRole("radio", { name: /not the same money/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));

    expect(rejectMutate).not.toHaveBeenCalled();
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
  });
});
