import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { RefundChecksTable, type RefundCheck } from "@/components/reconciliation/refund-checks";

const row: RefundCheck = {
  finding_id: "finding", run_id: "run", order_reference: "R123456789", entity: "2", currency: "EUR",
  source_amount: "2546.00", netsuite_amount: null, difference: null, status: "not_verified",
  historical_order_reconciled: true, observed_at: "2026-10-09T17:29:18Z",
};

describe("refund checks", () => {
  it("keeps a new refund visible after historical order reconciliation and never turns unknown into zero", () => {
    render(<RefundChecksTable items={[row]} />);
    expect(screen.getByText("R123456789")).toBeInTheDocument();
    expect(screen.getByText(/Order previously reconciled/)).toBeInTheDocument();
    expect(screen.getAllByText("Not verified")).toHaveLength(3);
    expect(screen.queryByText("EUR 0.00")).not.toBeInTheDocument();
    expect(screen.getByText("EUR 2,546.00")).toBeInTheDocument();
  });
  it("shows only the outstanding gap while retaining cumulative evidence amounts and its currency", () => {
    render(<RefundChecksTable items={[{...row, source_amount: "709.00", netsuite_amount: "644.00", difference: "65.00", status: "difference"}]} />);
    expect(screen.getByText("EUR 709.00")).toBeInTheDocument();
    expect(screen.getByText("EUR 644.00")).toBeInTheDocument();
    expect(screen.getByText("EUR 65.00")).toBeInTheDocument();
  });
});
