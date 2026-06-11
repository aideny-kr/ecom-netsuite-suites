import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { CloseChecklist } from "@/components/reconciliation/close-checklist";
import type { ReconCloseReadiness, ReconRun } from "@/lib/types";

// Mutable hook state so each test drives the PERIOD-scoped readiness (R3-A:
// the checks key on GET /close-readiness/{period} — aggregated over EVERY run
// the close will touch — never the selected run's bucket summary).
let mockReadiness: ReconCloseReadiness | undefined;
const closePeriodMutate = vi.fn();
const useCloseReadinessSpy = vi.fn();

vi.mock("@/hooks/use-reconciliation", () => ({
  useClosePeriod: () => ({ mutate: closePeriodMutate, isPending: false }),
  useCloseReadiness: (period: string | null) => {
    useCloseReadinessSpy(period);
    return { data: mockReadiness, isLoading: mockReadiness === undefined };
  },
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

/** All-zero, single-run readiness unless overridden. */
function makeReadiness(
  overrides: Partial<ReconCloseReadiness> = {},
): ReconCloseReadiness {
  return {
    period: "2026-05",
    runs_in_scope: 1,
    open_exceptions: 0,
    suggested: 0,
    left_for_review: 0,
    ...overrides,
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

beforeEach(() => {
  mockReadiness = makeReadiness();
  closePeriodMutate.mockReset();
  useCloseReadinessSpy.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CloseChecklist — period-readiness-driven auto-checks", () => {
  it("renders the six steps in order (material variances between approve and export)", () => {
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    const ids = STEP_IDS.map((id) => stepRow(id));
    // DOM order: each step row precedes the next.
    for (let i = 0; i < ids.length - 1; i++) {
      expect(
        ids[i].compareDocumentPosition(ids[i + 1]) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    }
  });

  it("requests readiness for the period it gates", () => {
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    expect(useCloseReadinessSpy).toHaveBeenCalledWith("2026-05");
  });

  it("open_exceptions > 0 leaves Review Exceptions incomplete", () => {
    mockReadiness = makeReadiness({ open_exceptions: 3 });
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    expect(isStepComplete("review_exceptions")).toBe(false);
    expect(isStepComplete("approve_matches")).toBe(true);
    expect(isStepComplete("review_material_variances")).toBe(true);
  });

  it("suggested > 0 leaves Approve Suggested Matches incomplete", () => {
    mockReadiness = makeReadiness({ suggested: 2 });
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    expect(isStepComplete("approve_matches")).toBe(false);
    expect(isStepComplete("review_exceptions")).toBe(true);
  });

  it("left_for_review > 0 leaves Review Material Variances incomplete and gates the lock", () => {
    mockReadiness = makeReadiness({ left_for_review: 1 });
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    expect(isStepComplete("review_material_variances")).toBe(false);
    // All other prereqs complete (export is manual) — the lock must still be
    // gated by the material-variance step alone.
    toggleExportEvidence();
    expect(lockButton()).toBeDisabled();
  });

  it("blocks the checklist when ANOTHER run in the period is unready (R3-A)", () => {
    // The selected run is clean, but the period readiness aggregates over a
    // second in-scope run that still has suggested + material rows — closing
    // would freeze them behind the closed-run guard without review.
    mockReadiness = makeReadiness({
      runs_in_scope: 2,
      suggested: 2,
      left_for_review: 1,
    });
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    expect(isStepComplete("approve_matches")).toBe(false);
    expect(isStepComplete("review_material_variances")).toBe(false);
    toggleExportEvidence();
    expect(lockButton()).toBeDisabled();
  });

  it("all-zero counts + completed run enables the lock once export is confirmed (single-run unchanged)", () => {
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    expect(lockButton()).toBeDisabled(); // export still manual
    toggleExportEvidence();
    expect(lockButton()).toBeEnabled();
  });
});

describe("CloseChecklist — lock confirm dialog", () => {
  it("mentions the run count when the close will touch more than one run", () => {
    mockReadiness = makeReadiness({ runs_in_scope: 2 });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    toggleExportEvidence();
    fireEvent.click(lockButton());
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy.mock.calls[0][0]).toContain(
      "This will close 2 runs in 2026-05",
    );
    expect(closePeriodMutate).toHaveBeenCalledWith("2026-05");
  });

  it("does not mention a run count for a single-run period", () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    toggleExportEvidence();
    fireEvent.click(lockButton());
    expect(confirmSpy.mock.calls[0][0]).not.toContain("runs in");
    // confirm returned false — the close must not fire.
    expect(closePeriodMutate).not.toHaveBeenCalled();
  });
});

describe("CloseChecklist — fail closed while readiness is loading/missing", () => {
  it("undefined readiness leaves every auto-check incomplete and the lock gated", () => {
    mockReadiness = undefined;
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    expect(isStepComplete("review_exceptions")).toBe(false);
    expect(isStepComplete("approve_matches")).toBe(false);
    expect(isStepComplete("review_material_variances")).toBe(false);
    toggleExportEvidence();
    expect(lockButton()).toBeDisabled();
  });

  it("run_recon still reflects the run status even without readiness", () => {
    mockReadiness = undefined;
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    expect(isStepComplete("run_recon")).toBe(true);
  });
});

describe("CloseChecklist — manual toggles unchanged (HITL semantics)", () => {
  it("manual toggle overrides an incomplete auto-check", () => {
    mockReadiness = makeReadiness({ left_for_review: 1 });
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    expect(isStepComplete("review_material_variances")).toBe(false);
    fireEvent.click(within(stepRow("review_material_variances")).getByRole("button"));
    expect(isStepComplete("review_material_variances")).toBe(true);
  });

  it("manual toggle is reversible", () => {
    mockReadiness = undefined;
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    fireEvent.click(within(stepRow("review_exceptions")).getByRole("button"));
    expect(isStepComplete("review_exceptions")).toBe(true);
    fireEvent.click(within(stepRow("review_exceptions")).getByRole("button"));
    expect(isStepComplete("review_exceptions")).toBe(false);
  });
});

describe("CloseChecklist — regressions", () => {
  it("a closed run shows the lock step complete", () => {
    render(<CloseChecklist run={makeRun({ status: "closed" })} period="2026-05" />);
    expect(isStepComplete("lock_period")).toBe(true);
    expect(isStepComplete("run_recon")).toBe(true);
  });

  it("a pending (not completed) run leaves run_recon incomplete", () => {
    render(<CloseChecklist run={makeRun({ status: "pending" })} period="2026-05" />);
    expect(isStepComplete("run_recon")).toBe(false);
  });

  it("lock button stays disabled until every prereq step is met", () => {
    render(<CloseChecklist run={makeRun()} period="2026-05" />);
    // Export Evidence Pack is manual and unchecked — lock must be disabled.
    expect(lockButton()).toBeDisabled();
    toggleExportEvidence();
    expect(lockButton()).toBeEnabled();
  });
});
