"use client";

import type { WriteConfirmationData } from "@/lib/types";
import { cn } from "@/lib/utils";

function label(value: unknown): string {
  if (value && typeof value === "object") {
    const v = value as Record<string, unknown>;
    return String(v.refName ?? v.name ?? v.id ?? "Not verified");
  }
  return value == null ? "Not verified" : String(value);
}

function amount(value: unknown, currency: string) {
  const n = value == null || value === "" ? NaN : Number(value);
  if (!Number.isFinite(n)) return "Not verified";
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
    }).format(n);
  } catch {
    return `${n.toFixed(2)} ${currency}`;
  }
}

export function AccountingConfirmationCard({
  data,
  onConfirm,
  onReject,
  disabled = false,
  readOnly = false,
}: {
  data: WriteConfirmationData;
  onConfirm: () => void;
  onReject: () => void;
  disabled?: boolean;
  readOnly?: boolean;
}) {
  const p = data.accounting_review!;
  const pending = data.status === "pending";
  const verified =
    data.status === "approved" &&
    data.accounting_verification?.status === "verified";
  const blocked = Boolean(
    data.invariant_errors?.length ||
      data.unfillable_line_fields?.length ||
      data.editable_slots?.length,
  );
  const currency =
    typeof p.before.currency_code === "string"
      ? p.before.currency_code
      : "Currency unknown";
  const state = verified
    ? "Executed · verified"
    : {
        pending: blocked ? "Needs review" : "Awaiting approval",
        executing: "Executing · checking results",
        approved: "Executed · verification needed",
        rejected: "Rejected",
        failed: "Needs review",
        indeterminate: "Outcome unconfirmed",
      }[data.status];
  const after = verified
    ? data.accounting_verification?.invoice
    : p.expected_after;
  const locked = p.period.arLocked === true || p.period.allLocked === true;
  return (
    <article
      className={cn(
        "overflow-hidden rounded-2xl border bg-card text-[13px]",
        verified ? "border-emerald-500/50" : "border-amber-500/40",
      )}
      aria-label={`Accounting correction ${p.order_reference}`}
    >
      <div className="space-y-4 p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-xl font-semibold tracking-tight">
              {verified ? "Invoice tax corrected" : "Correct invoice tax"}
            </h3>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              Order {p.order_reference} · Invoice {label(p.before.tranId)} (#
              {p.record_id})<br />
              {label(p.before.subsidiary)} · NetSuite{" "}
              {data.target_environment?.toLowerCase() ||
                "environment unverified"}{" "}
              · {p.scope.netsuite_account_id} · {currency}
            </p>
          </div>
          <span
            role="status"
            className={cn(
              "rounded-md px-2.5 py-1 text-xs font-medium",
              verified
                ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                : "bg-amber-500/10 text-amber-700 dark:text-amber-400",
            )}
          >
            {state}
          </span>
        </div>
        <p className="leading-relaxed text-muted-foreground">
          {verified ? (
            "The approved invoice update succeeded. Fresh NetSuite reads confirmed the invoice amounts and general ledger entries."
          ) : (
            <>
              <strong className="text-foreground">Why this change: </strong>The
              invoice rate uses the tax-inclusive total as its denominator.
              Recalculate the effective rate using the net subtotal to match the
              source platform’s finalized tax.
            </>
          )}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-right tabular-nums">
            <thead>
              <tr className="border-b text-xs text-muted-foreground">
                <th className="py-3 text-left font-medium">Financial impact</th>
                <th className="px-2 font-medium">Before</th>
                <th className="px-2 font-medium">
                  {verified ? "Verified after" : "Proposed"}
                </th>
                <th className="font-medium">Change</th>
              </tr>
            </thead>
            <tbody>
              {[
                ["Tax / VAT", "taxTotal"],
                ["Invoice total", "total"],
              ].map(([name, key]) => {
                const next = after?.[key as keyof typeof after];
                const delta =
                  next != null && p.before[key] != null
                    ? Number(next) - Number(p.before[key])
                    : NaN;
                return (
                  <tr key={key} className="border-b">
                    <th className="py-3 text-left font-medium">{name}</th>
                    <td className="whitespace-nowrap px-2 text-muted-foreground">
                      {amount(p.before[key], currency)}
                    </td>
                    <td className="whitespace-nowrap px-2 font-semibold">
                      {amount(next, currency)}
                    </td>
                    <td className="whitespace-nowrap font-semibold">
                      {delta > 0 ? "+" : ""}
                      {amount(delta, currency)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="mt-2 text-xs text-muted-foreground">
            The total includes tax. These changes represent one difference.
          </p>
        </div>
        {pending && (
          <div className="space-y-2 rounded-lg border border-amber-500/30 border-l-4 bg-amber-500/5 p-4">
            <h4 className="font-semibold">Before you approve</h4>
            <ul className="list-disc space-y-2 pl-4 text-xs leading-relaxed">
              <li>
                Approval confirms the source tax basis and retaining{" "}
                {p.tax_account_name || `tax account ${p.tax_account}`} /{" "}
                {label(p.tax_item.taxAgency)}. Their jurisdictional
                classification and ship-to country have not been independently
                verified.
              </li>
              <li>
                {label(p.period.periodName ?? p.before.postingPeriod)}:{" "}
                {locked
                  ? "locked, but not closed. The update needs the connected role’s existing override permission"
                  : "not closed at the last check; controls will be checked again"}
                . This changes posted receivables and tax.
              </li>
            </ul>
          </div>
        )}
        {verified && (
          <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-4">
            <p className="font-semibold text-emerald-600 dark:text-emerald-400">
              ✓ Invoice and GL verified
            </p>
            <p className="mt-1 text-xs leading-relaxed">
              Invoice balance:{" "}
              {amount(
                data.accounting_verification?.invoice?.amountRemaining,
                currency,
              )}
              . Your approval, before/after values, execution receipt and
              verification are recorded in the database audit log.
            </p>
          </div>
        )}
        {!pending && !verified && (
          <p role="status" className="rounded-lg border p-3 leading-relaxed">
            {data.status === "executing"
              ? "The approved update is in progress. Wait for independent verification."
              : data.status === "rejected"
                ? "This proposal was rejected."
                : "The invoice correction is not verified. Check the recorded outcome before attempting another write."}
            {data.error && ` ${data.error}`}
            {data.accounting_verification?.reason &&
              ` ${data.accounting_verification.reason}`}
          </p>
        )}
        {(blocked || data.unvalidated) && (
          <div
            role="alert"
            className="rounded-lg border border-amber-500/30 p-3"
          >
            {[
              ...(data.invariant_errors || []),
              ...(data.unfillable_line_fields || []),
            ].map((error) => (
              <p key={error}>{error}</p>
            ))}
            {data.unvalidated && (
              <p>
                Native field validation was unavailable. Review the exact fields
                before approving.
              </p>
            )}
          </div>
        )}
        <div className="divide-y border-y">
          <details className="py-3">
            <summary className="cursor-pointer font-medium">
              Exact field change
            </summary>
            <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-xs">
              <dt>Effective tax rate (taxRate)</dt>
              <dd className="font-mono">
                {label(p.before.taxRate)}% →{" "}
                {label(data.proposed_fields.taxRate)}%
              </dd>
              <dt>Calculation</dt>
              <dd>
                Finalized source VAT ÷ net subtotal × 100, rounded to 7
                decimals.
              </dd>
            </dl>
            <p className="mt-3 text-xs text-muted-foreground">
              This restores the integration’s effective rate; it does not
              establish a new statutory tax rate.
            </p>
          </details>
          <details className="py-3">
            <summary className="cursor-pointer font-medium">
              Accounting details and evidence
            </summary>
            <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-xs">
              <dt>Tax account / agency</dt>
              <dd>
                {p.tax_account_name || p.tax_account} /{" "}
                {label(p.tax_item.taxAgency)}
              </dd>
              <dt>Receivables</dt>
              <dd>{p.ar_account_name || p.ar_account}</dd>
              <dt>Posting period / book</dt>
              <dd>
                {label(p.before.postingPeriod)} · Book {p.accounting_book}
              </dd>
            </dl>
            <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
              {p.approval_basis}
            </p>
          </details>
        </div>
        <p className="text-xs leading-relaxed text-muted-foreground">
          <strong className="font-medium text-foreground">
            Scope: this invoice only.
          </strong>{" "}
          Sales-order reconciliation and deposit / cash settlement require
          separate verification and, if another write is needed, another
          approval.
        </p>
      </div>
      {!readOnly && (
        <div className="flex flex-wrap items-center justify-between gap-4 border-t bg-muted/20 px-5 py-4 sm:px-6">
          <p className="text-xs leading-relaxed text-muted-foreground">
            {pending
              ? "Fresh checks run again before the update. Approval and results are audited."
              : verified
                ? "Invoice correction verified. Full case settlement remains separate."
                : state}
          </p>
          {pending && (
            <div className="flex gap-2">
              <button
                className="rounded-lg border px-4 py-2 text-xs font-medium disabled:opacity-50"
                disabled={disabled}
                onClick={onReject}
              >
                Reject
              </button>
              <button
                className="rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-primary-foreground disabled:opacity-50"
                disabled={disabled || blocked}
                onClick={onConfirm}
              >
                Approve invoice correction
              </button>
            </div>
          )}
        </div>
      )}
    </article>
  );
}
