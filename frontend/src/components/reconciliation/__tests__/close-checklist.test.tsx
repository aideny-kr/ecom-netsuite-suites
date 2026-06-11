import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { CloseChecklist } from "@/components/reconciliation/close-checklist";
import type { ReconRun, ReconResult } from "@/lib/types";

vi.mock("@/hooks/use-reconciliation", () => ({
  useClosePeriod: () => ({ mutate: vi.fn(), isPending: false }),
}));

function makeRun(overrides: Partial<ReconRun> = {}): ReconRun {
  return {
    id: "run-1",
    tenant_id: "tenant-1",
    date_from: "2026-05-01",
    date_to: "2026-05-31",
    subsidiary_id: null,
    status: "completed",
    total_payouts: 1,
    total_deposits: 1,
    matched_count: 1,
    exception_count: 0,
    unmatched_count: 0,
    total_variance: 0,
    created_at: "2026-06-01T00:00:00Z",
    ...overrides,
  };
}

function makeResult(overrides: Partial<ReconResult> = {}): ReconResult {
  return {
    id: "res-1",
    run_id: "run-1",
    payout_id: null,
    deposit_id: "dep-1",
    match_type: "deterministic",
    confidence: 0.95,
    status: "approved",
    stripe_amount: 100,
    netsuite_amount: 100,
    variance_amount: 0,
    variance_type: null,
    variance_explanation: null,
    currency: "USD",
    match_rule: null,
    evidence: null,
    approved_by: null,
    approved_at: null,
    created_at: "2026-06-01T00:00:00Z",
    bucket: "matches",
    ...overrides,
  };
}

/** Row container for a checklist step (carries bg-green-50 when complete). */
function rowFor(label: string | RegExp): HTMLElement {
  return screen.getByText(label).parentElement!.parentElement! as HTMLElement;
}

function isStepComplete(label: string | RegExp): boolean {
  return rowFor(label).className.includes("bg-green-50");
}

function lockButton(): HTMLElement {
  return screen.getByRole("button", { name: /lock period/i });
}

/** The export-evidence step is manual-only; toggle it so the lock gate is
 *  isolated to the step under test. */
function toggleExportEvidence() {
  fireEvent.click(within(rowFor("Export Evidence Pack")).getByRole("button"));
}

describe("CloseChecklist — review_material_variances step", () => {
  it("renders the Review Material Variances step between approve and export", () => {
    render(<CloseChecklist run={makeRun()} results={[]} period="2026-05" />);
    const labels = screen
      .getAllByText(
        /Run Reconciliation|Review Exceptions|Approve Suggested Matches|Review Material Variances|Export Evidence Pack|Lock Period/,
        // Scope to the step-label <p> elements (the lock BUTTON also says "Lock Period").
        { selector: "p" },
      )
      .map((el) => el.textContent);
    expect(labels).toEqual([
      "Run Reconciliation",
      "Review Exceptions",
      "Approve Suggested Matches",
      "Review Material Variances",
      "Export Evidence Pack",
      "Lock Period",
    ]);
  });

  it("auto_matched + needs_review row leaves the step incomplete and gates the lock", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        results={[makeResult({ status: "auto_matched", bucket: "needs_review" })]}
        period="2026-05"
      />,
    );
    expect(isStepComplete("Review Material Variances")).toBe(false);
    // All other prereqs complete (export is manual) — the lock must still be
    // gated by the material-variance step alone.
    toggleExportEvidence();
    expect(lockButton()).toBeDisabled();
  });

  it("same row with status approved completes the step and ungates the lock", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        results={[makeResult({ status: "approved", bucket: "needs_review" })]}
        period="2026-05"
      />,
    );
    expect(isStepComplete("Review Material Variances")).toBe(true);
    toggleExportEvidence();
    expect(lockButton()).toBeEnabled();
  });

  it("a row without bucket does not crash and does not block the step", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        results={[makeResult({ status: "auto_matched", bucket: undefined })]}
        period="2026-05"
      />,
    );
    expect(isStepComplete("Review Material Variances")).toBe(true);
  });

  it("manual toggle overrides the step (HITL checklist semantics preserved)", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        results={[makeResult({ status: "auto_matched", bucket: "needs_review" })]}
        period="2026-05"
      />,
    );
    expect(isStepComplete("Review Material Variances")).toBe(false);
    fireEvent.click(
      within(rowFor("Review Material Variances")).getByRole("button"),
    );
    expect(isStepComplete("Review Material Variances")).toBe(true);
  });
});

describe("CloseChecklist — existing steps unchanged (regressions)", () => {
  it("pending + unmatched does not block Review Exceptions", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        results={[
          makeResult({ status: "pending", match_type: "unmatched", bucket: "needs_review" }),
        ]}
        period="2026-05"
      />,
    );
    expect(isStepComplete("Review Exceptions")).toBe(true);
  });

  it("pending + matched still blocks Review Exceptions", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        results={[makeResult({ status: "pending", match_type: "fuzzy" })]}
        period="2026-05"
      />,
    );
    expect(isStepComplete("Review Exceptions")).toBe(false);
  });

  it("suggested still blocks Approve Suggested Matches", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        results={[makeResult({ status: "suggested" })]}
        period="2026-05"
      />,
    );
    expect(isStepComplete("Approve Suggested Matches")).toBe(false);
  });

  it("lock button stays disabled until every prereq step is met", () => {
    render(<CloseChecklist run={makeRun()} results={[]} period="2026-05" />);
    // Export Evidence Pack is manual and unchecked — lock must be disabled.
    expect(lockButton()).toBeDisabled();
    toggleExportEvidence();
    expect(lockButton()).toBeEnabled();
  });
});
