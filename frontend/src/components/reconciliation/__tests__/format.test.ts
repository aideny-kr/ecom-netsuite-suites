import { describe, it, expect } from "vitest";
import { money, roundHalfUpString, fxChip } from "@/components/reconciliation/format";
import type { ReconResolutionProposal } from "@/lib/types";

describe("money", () => {
  it("formats a decimal string as USD by default", () => {
    expect(money("1284.55")).toBe("$1,284.55");
  });

  it("formats in the given currency", () => {
    expect(money("500.00", "EUR")).toBe("€500.00");
  });

  it("renders a dash for null/undefined instead of throwing or showing $0.00", () => {
    expect(money(null, "USD")).toBe("—");
    expect(money(undefined, "USD")).toBe("—");
  });
});

describe("roundHalfUpString", () => {
  // RED-first (item 2a): Number("0.100350").toFixed(4) === "0.1003" because
  // 0.10035 has no exact IEEE-754 double representation — the naive
  // toFixed() approach silently mis-rounds a recorded rate down instead of
  // to the mathematically correct "0.1004".
  it("rounds a half-way decimal string up, unlike Number.prototype.toFixed", () => {
    expect(Number("0.100350").toFixed(4)).toBe("0.1003"); // documents the float bug this helper avoids
    expect(roundHalfUpString("0.100350", 4)).toBe("0.1004");
  });

  it("rounds down when the dropped digit is below 5", () => {
    expect(roundHalfUpString("0.12340", 4)).toBe("0.1234");
    expect(roundHalfUpString("0.123449", 4)).toBe("0.1234");
  });

  it("carries a round-up through trailing 9s", () => {
    expect(roundHalfUpString("1.99995", 4)).toBe("2.0000");
  });

  it("accepts a plain number", () => {
    expect(roundHalfUpString(0.71135, 4)).toBe("0.7114");
  });

  it("preserves sign for negative values", () => {
    expect(roundHalfUpString("-0.100350", 4)).toBe("-0.1004");
  });
});

function baseProposal(overrides: Partial<ReconResolutionProposal>): ReconResolutionProposal {
  return {
    id: "p1",
    run_id: "r1",
    result_id: "res1",
    root_cause: "fees",
    action: "book_fee_line",
    booking_vehicle: "deposit",
    group_key: "fees:book_fee_line:deposit",
    source: "planner",
    narrative: "n",
    proposed_amount: "9.00",
    currency: "USD",
    above_materiality: false,
    status: "proposed",
    failure_reason: null,
    correlation_id: null,
    created_at: "2026-07-06T00:00:00Z",
    order_reference: "R1",
    stripe_charge_id: "ch_1",
    netsuite_internal_id: "1",
    netsuite_record_type: "custdep",
    stripe_amount: null,
    netsuite_amount: null,
    variance_amount: null,
    deposit_transaction_currency: null,
    deposit_foreign_amount: null,
    deposit_exchange_rate: null,
    result_status: "pending",
    ...overrides,
  };
}

describe("fxChip", () => {
  it("returns null when there is no deposit transaction currency", () => {
    expect(fxChip(baseProposal({}))).toBeNull();
  });

  it("returns null when the transaction currency matches the proposal currency", () => {
    expect(
      fxChip(baseProposal({ currency: "EUR", deposit_transaction_currency: "EUR" }))
    ).toBeNull();
  });

  it("prefers the recorded exchange_rate, rounded half-up, and labels it as recorded", () => {
    const chip = fxChip(
      baseProposal({
        deposit_transaction_currency: "EUR",
        deposit_exchange_rate: "0.100350",
      })
    );
    expect(chip).toEqual({
      label: "EUR @ 0.1004",
      title: "Booked in EUR at 0.1004 (recorded rate)",
    });
  });

  // RED-first (item 1, mutation-detecting): the old implementation computed
  // netsuite_amount / stripe_amount — a variance ratio, not an FX rate.
  // These amounts are chosen so the wrong formula (4782.06 / 5000.00 =
  // 0.9564) and the correct one (4782.06 / 6722.37 = 0.7114) are clearly
  // different numbers — a regression back to the old formula fails this.
  it("falls back to netsuite_amount / deposit_foreign_amount (never stripe_amount) when exchange_rate is null, marked with an estimated title", () => {
    const chip = fxChip(
      baseProposal({
        deposit_transaction_currency: "EUR",
        deposit_exchange_rate: null,
        deposit_foreign_amount: "6722.37",
        netsuite_amount: "4782.06",
        stripe_amount: "5000.00",
      })
    );
    expect(chip).toEqual({
      label: "EUR ≈ 0.7114",
      title: "Booked in EUR, ≈0.7114 estimated from booked amounts (no recorded rate)",
    });
  });

  it("omits the rate (currency-only chip) when neither exchange_rate nor foreign_amount is available", () => {
    const chip = fxChip(
      baseProposal({
        deposit_transaction_currency: "EUR",
        deposit_exchange_rate: null,
        deposit_foreign_amount: null,
        netsuite_amount: "991.00",
        stripe_amount: "1000.00",
      })
    );
    expect(chip).toEqual({ label: "EUR", title: "Booked in EUR" });
  });

  it("omits the rate when foreign_amount is zero (division guard)", () => {
    const chip = fxChip(
      baseProposal({
        deposit_transaction_currency: "EUR",
        deposit_exchange_rate: null,
        deposit_foreign_amount: "0",
        netsuite_amount: "991.00",
      })
    );
    expect(chip).toEqual({ label: "EUR", title: "Booked in EUR" });
  });

  it("omits the rate when netsuite_amount is null even though foreign_amount is present", () => {
    const chip = fxChip(
      baseProposal({
        deposit_transaction_currency: "EUR",
        deposit_exchange_rate: null,
        deposit_foreign_amount: "827.00",
        netsuite_amount: null,
      })
    );
    expect(chip).toEqual({ label: "EUR", title: "Booked in EUR" });
  });
});
