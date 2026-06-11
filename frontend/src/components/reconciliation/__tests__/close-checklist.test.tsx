import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { CloseChecklist } from "@/components/reconciliation/close-checklist";
import type { ReconBucketSummary, ReconCloseReadiness, ReconRun } from "@/lib/types";

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

/** Summary with all-zero close_readiness unless overridden — the auto-checks
 *  are driven by these server-computed full-run counts, never a results page. */
function makeSummary(
  closeReadiness: Partial<ReconCloseReadiness> = {},
): ReconBucketSummary {
  const zero = { count: 0, total_variance: 0 };
  return {
    run_id: "run-1",
    matches: zero,
    rules: zero,
    auto_classifications: zero,
    needs_review: zero,
    close_readiness: {
      open_exceptions: 0,
      suggested: 0,
      left_for_review: 0,
      ...closeReadiness,
    },
  };
}

const STEP_IDS = [
  "run_recon",
  "review_exceptions",
  "approve_matches",
  "review_material_variances",
  "export_evidence",
  "lock_period",
] as const;

function stepRow(id: string): HTMLElement {
  return screen.getByTestId(id);
}

function isStepComplete(id: string): boolean {
  return stepRow(id).getAttribute("data-complete") === "true";
}

function lockButton(): HTMLElement {
  return screen.getByRole("button", { name: /lock period/i });
}

/** The export-evidence step is manual-only; toggle it so the lock gate is
 *  isolated to the step under test. */
function toggleExportEvidence() {
  fireEvent.click(within(stepRow("export_evidence")).getByRole("button"));
}

describe("CloseChecklist — summary-driven auto-checks", () => {
  it("renders the six steps in order (material variances between approve and export)", () => {
    render(<CloseChecklist run={makeRun()} summary={makeSummary()} period="2026-05" />);
    const ids = STEP_IDS.map((id) => stepRow(id));
    // DOM order: each step row precedes the next.
    for (let i = 0; i < ids.length - 1; i++) {
      expect(
        ids[i].compareDocumentPosition(ids[i + 1]) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    }
  });

  it("open_exceptions > 0 leaves Review Exceptions incomplete", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        summary={makeSummary({ open_exceptions: 3 })}
        period="2026-05"
      />,
    );
    expect(isStepComplete("review_exceptions")).toBe(false);
    expect(isStepComplete("approve_matches")).toBe(true);
    expect(isStepComplete("review_material_variances")).toBe(true);
  });

  it("suggested > 0 leaves Approve Suggested Matches incomplete", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        summary={makeSummary({ suggested: 2 })}
        period="2026-05"
      />,
    );
    expect(isStepComplete("approve_matches")).toBe(false);
    expect(isStepComplete("review_exceptions")).toBe(true);
  });

  it("left_for_review > 0 leaves Review Material Variances incomplete and gates the lock", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        summary={makeSummary({ left_for_review: 1 })}
        period="2026-05"
      />,
    );
    expect(isStepComplete("review_material_variances")).toBe(false);
    // All other prereqs complete (export is manual) — the lock must still be
    // gated by the material-variance step alone.
    toggleExportEvidence();
    expect(lockButton()).toBeDisabled();
  });

  it("all-zero counts + completed run enables the lock once export is confirmed", () => {
    render(<CloseChecklist run={makeRun()} summary={makeSummary()} period="2026-05" />);
    expect(lockButton()).toBeDisabled(); // export still manual
    toggleExportEvidence();
    expect(lockButton()).toBeEnabled();
  });
});

describe("CloseChecklist — fail closed while summary is loading/missing", () => {
  it("undefined summary leaves every auto-check incomplete and the lock gated", () => {
    render(<CloseChecklist run={makeRun()} summary={undefined} period="2026-05" />);
    expect(isStepComplete("review_exceptions")).toBe(false);
    expect(isStepComplete("approve_matches")).toBe(false);
    expect(isStepComplete("review_material_variances")).toBe(false);
    toggleExportEvidence();
    expect(lockButton()).toBeDisabled();
  });

  it("summary without close_readiness (deploy skew) also fails closed", () => {
    const summary = { ...makeSummary(), close_readiness: undefined };
    render(<CloseChecklist run={makeRun()} summary={summary} period="2026-05" />);
    expect(isStepComplete("review_exceptions")).toBe(false);
    expect(isStepComplete("approve_matches")).toBe(false);
    expect(isStepComplete("review_material_variances")).toBe(false);
  });

  it("run_recon still reflects the run status even without a summary", () => {
    render(<CloseChecklist run={makeRun()} summary={undefined} period="2026-05" />);
    expect(isStepComplete("run_recon")).toBe(true);
  });
});

describe("CloseChecklist — manual toggles unchanged (HITL semantics)", () => {
  it("manual toggle overrides an incomplete auto-check", () => {
    render(
      <CloseChecklist
        run={makeRun()}
        summary={makeSummary({ left_for_review: 1 })}
        period="2026-05"
      />,
    );
    expect(isStepComplete("review_material_variances")).toBe(false);
    fireEvent.click(within(stepRow("review_material_variances")).getByRole("button"));
    expect(isStepComplete("review_material_variances")).toBe(true);
  });

  it("manual toggle is reversible", () => {
    render(<CloseChecklist run={makeRun()} summary={undefined} period="2026-05" />);
    fireEvent.click(within(stepRow("review_exceptions")).getByRole("button"));
    expect(isStepComplete("review_exceptions")).toBe(true);
    fireEvent.click(within(stepRow("review_exceptions")).getByRole("button"));
    expect(isStepComplete("review_exceptions")).toBe(false);
  });
});

describe("CloseChecklist — regressions", () => {
  it("a closed run shows the lock step complete", () => {
    render(
      <CloseChecklist
        run={makeRun({ status: "closed" })}
        summary={makeSummary()}
        period="2026-05"
      />,
    );
    expect(isStepComplete("lock_period")).toBe(true);
    expect(isStepComplete("run_recon")).toBe(true);
  });

  it("a pending (not completed) run leaves run_recon incomplete", () => {
    render(
      <CloseChecklist
        run={makeRun({ status: "pending" })}
        summary={makeSummary()}
        period="2026-05"
      />,
    );
    expect(isStepComplete("run_recon")).toBe(false);
  });

  it("lock button stays disabled until every prereq step is met", () => {
    render(<CloseChecklist run={makeRun()} summary={makeSummary()} period="2026-05" />);
    // Export Evidence Pack is manual and unchecked — lock must be disabled.
    expect(lockButton()).toBeDisabled();
    toggleExportEvidence();
    expect(lockButton()).toBeEnabled();
  });
});
