import type { ReconResolutionProposal } from "@/lib/types";

/** Shared currency formatter for the reconciliation worksheets — null/undefined
 * renders as "—" rather than "$0.00" or throwing. */
export function money(amount: string | number | null | undefined, currency: string = "USD"): string {
  if (amount === null || amount === undefined) return "—";
  return Number(amount).toLocaleString("en-US", { style: "currency", currency: currency || "USD" });
}

/** Rounds a decimal amount to `decimals` places using half-up (ties away from
 * zero) rounding, computed on its string representation rather than via
 * `Number.prototype.toFixed` — `(0.10035).toFixed(4)` returns "0.1003"
 * because 0.10035 has no exact IEEE-754 double representation. Accepts a
 * plain number too (stringified first) for the implied-rate case, where the
 * value is already a computed float. */
export function roundHalfUpString(value: string | number, decimals: number): string {
  const raw = (typeof value === "number" ? value.toString() : value).trim();
  const negative = raw.startsWith("-");
  const unsigned = negative ? raw.slice(1) : raw;
  const [intPart, fracPart = ""] = unsigned.split(".");
  const padded = fracPart.padEnd(decimals + 1, "0");
  const keep = padded.slice(0, decimals);
  const roundUp = Number(padded.charAt(decimals) || "0") >= 5;

  const digits = `${intPart}${keep}`.split("").map(Number);
  if (roundUp) {
    let i = digits.length - 1;
    while (i >= 0) {
      digits[i] += 1;
      if (digits[i] < 10) break;
      digits[i] = 0;
      i -= 1;
    }
    if (i < 0) digits.unshift(1);
  }

  const digitsStr = digits.join("");
  const intLen = digitsStr.length - decimals;
  const newInt = digitsStr.slice(0, intLen) || "0";
  const newFrac = decimals > 0 ? digitsStr.slice(intLen) : "";
  const magnitude = decimals > 0 ? `${newInt}.${newFrac}` : newInt;
  return negative && Number(magnitude) !== 0 ? `-${magnitude}` : magnitude;
}

/** Phase C (FX mark-only): a muted "EUR @ 0.9231" chip next to the NetSuite
 * ID, shown only when the matched deposit's transaction currency differs
 * from the proposal's currency.
 *
 * Prefers the recorded `deposit_exchange_rate` — rendered honestly as a
 * recorded rate. Falls back to the *implied* rate, `netsuite_amount /
 * deposit_foreign_amount` (base currency per foreign unit — the same
 * convention as the recorded exchange_rate: foreign_amount × exchange_rate =
 * amount), marked with a `≈` prefix and an "estimated" title when no rate is
 * on file. `stripe_amount` never enters this calculation — dividing
 * netsuite_amount by stripe_amount is a variance ratio (~1.0), not an FX
 * rate. Omits the rate entirely (currency-only chip) when neither is
 * available. Mark-only — this never changes the proposal's classification. */
export function fxChip(p: ReconResolutionProposal): { label: string; title: string } | null {
  const txnCurrency = p.deposit_transaction_currency;
  if (!txnCurrency || txnCurrency === p.currency) return null;

  if (p.deposit_exchange_rate !== null) {
    const rateLabel = roundHalfUpString(p.deposit_exchange_rate, 4);
    return {
      label: `${txnCurrency} @ ${rateLabel}`,
      title: `Booked in ${txnCurrency} at ${rateLabel} (recorded rate)`,
    };
  }

  const foreign = p.deposit_foreign_amount !== null ? Number(p.deposit_foreign_amount) : null;
  const base = p.netsuite_amount !== null ? Number(p.netsuite_amount) : null;
  if (foreign !== null && base !== null && foreign !== 0) {
    const implied = base / foreign;
    if (Number.isFinite(implied)) {
      const rateLabel = roundHalfUpString(implied, 4);
      return {
        label: `${txnCurrency} ≈ ${rateLabel}`,
        title: `Booked in ${txnCurrency}, ≈${rateLabel} estimated from booked amounts (no recorded rate)`,
      };
    }
  }

  return { label: txnCurrency, title: `Booked in ${txnCurrency}` };
}
