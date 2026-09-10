"use client";

import { useState } from "react";
import type { SalesCreditReview, WriteConfirmationData } from "@/lib/types";

function label(value: unknown): string {
  if (value && typeof value === "object") {
    const ref = value as Record<string, unknown>;
    return String(ref.refName ?? ref.name ?? ref.id ?? "Not verified");
  }
  return value == null ? "Not verified" : String(value);
}

export function creditAmount(value: unknown, currency: string): string {
  const n = value == null || value === "" ? NaN : Number(value);
  if (!Number.isFinite(n)) return "Not verified";
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency", currency, minimumFractionDigits: 2,
    }).format(n);
  } catch {
    return `${n.toFixed(2)} ${currency}`;
  }
}

export function SalesCreditConfirmationCard({
  data, proposal: p, onConfirm, onReject, disabled = false, readOnly = false, groupState,
}: {
  data: WriteConfirmationData;
  proposal: SalesCreditReview;
  onConfirm: () => void;
  onReject: () => void;
  disabled?: boolean;
  readOnly?: boolean;
  groupState?: WriteConfirmationData["status"];
}) {
  const [reviewed, setReviewed] = useState(false);
  const awaitingGroup = data.status === "pending" && groupState && groupState !== "pending";
  const pending = data.status === "pending" && !awaitingGroup;
  const verified = data.status === "approved" && data.accounting_verification?.status === "verified";
  const blocked = Boolean(data.invariant_errors?.length || data.unfillable_line_fields?.length || data.editable_slots?.length);
  const currency = p.profile.currency;
  const resolution = data.accounting_verification?.resolution;
  const credit = verified ? resolution?.credit_amount : p.expected_after.credit_total;
  const state = awaitingGroup
    ? groupState === "executing" ? "Awaiting result" : "Not submitted"
    : verified ? "Executed · verified" : {
      pending: blocked ? "Needs review" : "Awaiting approval",
      executing: "Executing · checking results",
      approved: "Executed · verification needed",
      rejected: "Rejected", failed: "Needs review", indeterminate: "Outcome unconfirmed",
    }[data.status];
  const rows: Array<[string, unknown, boolean?]> = [
    ["Original invoice total", p.before.total],
    [verified ? "Applied Sales Adjustments credit" : "Proposed Sales Adjustments credit", credit, true],
    ["Net invoice total", verified ? resolution?.net_invoice_total : p.expected_after.net_invoice_total],
    ["Source order total", p.source.total],
    ["Remaining variance", verified ? resolution?.remaining_variance : p.expected_after.remaining_variance],
    ["Tax impact of credit", verified ? resolution?.tax_amount : p.expected_after.credit_tax],
  ];
  return (
    <article className="overflow-hidden rounded-2xl border bg-card text-[13px]" aria-label={`Sales Adjustments credit ${p.order_reference}`}>
      <div className="space-y-4 p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-xl font-semibold tracking-tight">{verified ? "Sales Adjustments credit applied" : "Create a Sales Adjustments credit"}</h3>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              Order {p.order_reference} · Invoice {label(p.before.tranId)} (#{p.record_id})<br />
              {label(p.before.subsidiary)} · {currency} · NetSuite {data.target_environment?.toLowerCase() || "environment unverified"} {p.scope.netsuite_account_id}
            </p>
          </div>
          <span role="status" className="rounded-md bg-muted px-2.5 py-1 text-xs font-medium">{state}</span>
        </div>
        <p className="rounded-lg bg-muted/40 p-4 leading-relaxed">
          {verified
            ? `Credit ${label(resolution?.credit_memo_number ?? data.accounting_verification?.credit_memo_id)} covers the finalized source adjustment. Its application and general ledger entries were independently verified.`
            : <>The source order includes a finalized <strong>{p.profile.source_adjustment_label}</strong> adjustment. This proposal creates a non-taxable credit and applies it only to this invoice.</>}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full tabular-nums" aria-label="Invoice reconciliation">
            <thead><tr className="border-b text-xs text-muted-foreground"><th className="py-3 text-left font-medium">Financial impact · {currency}</th><th className="py-3 text-right font-medium">{verified ? "Verified result" : "Proposed result"}</th></tr></thead>
            <tbody>{rows.map(([name, value, reduction]) => (
              <tr key={name} className="border-b">
                <th className="py-3 pr-4 text-left font-medium">{name}</th>
                <td className="whitespace-nowrap py-3 text-right">{reduction && value != null ? "−" : ""}{creditAmount(value, currency)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        {!pending && !verified && (
          <p role="status" className="rounded-lg border p-3 leading-relaxed">
            {awaitingGroup
              ? groupState === "executing" ? "Group processing is in progress. This order has not recorded an execution result yet." : "This proposal was not submitted. Review the group’s recorded results."
              : data.status === "executing" ? "The approved credit is in progress. Wait for independent verification."
                : data.status === "rejected" ? "This proposal was rejected."
                  : "The credit and its application are not verified. Review the recorded outcome before attempting another write."}
            {data.error && ` ${data.error}`}{data.accounting_verification?.reason && ` ${data.accounting_verification.reason}`}
          </p>
        )}
        {(blocked || data.unvalidated) && (
          <div role="alert" className="rounded-lg border border-amber-500/30 p-3">
            {[...(data.invariant_errors || []), ...(data.unfillable_line_fields || [])].map(error => <p key={error}>{error}</p>)}
            {!!data.editable_slots?.length && <p>The exact credit fields are incomplete. Refresh the proposal.</p>}
            {data.unvalidated && <p>Native field validation was unavailable. Review the exact fields before approving.</p>}
          </div>
        )}
        <details className="border-y py-3">
          <summary className="cursor-pointer font-medium">Accounting impact and verification</summary>
          <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-2 text-xs sm:grid-cols-[auto_1fr]">
            <dt>Debit</dt><dd>{p.sales_adjustment_account_name || `Sales adjustments account ${p.sales_adjustment_account}`} · {creditAmount(credit, currency)}</dd>
            <dt>Credit</dt><dd>{p.ar_account_name || `Accounts Receivable account ${p.ar_account}`} · {creditAmount(credit, currency)}</dd>
            <dt>Item</dt><dd>Sales Adjustments ({p.profile.item_id}) · Non-taxable</dd>
            <dt>Apply only to</dt><dd>Invoice {label(p.before.tranId)} (#{p.record_id}) · {creditAmount(credit, currency)}</dd>
            <dt>Posting date / period</dt><dd>{label(p.proposed_fields.tranDate)} · {label(p.period.periodName ?? p.period.id)} · Book {p.accounting_book}</dd>
          </dl>
          <p className="mt-3 text-xs leading-relaxed text-muted-foreground">{p.approval_basis}</p>
          <details className="mt-3"><summary className="cursor-pointer text-xs">Exact credit fields</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-muted/40 p-3 text-xs">{JSON.stringify(p.proposed_fields, null, 2)}</pre></details>
        </details>
        <p className="text-xs leading-relaxed text-muted-foreground">
          {verified ? "Approval, approver identity, posting receipt and verification are recorded in the database audit log." : "Fresh evidence and duplicate-credit checks run again before posting. Approval, approver identity, posting receipt and verification are recorded in the database audit log."} No cash refund is issued. Bank and processor clearance remain separate checks.
        </p>
        {verified && data.accounting_recheck && (
          <p className="text-xs leading-relaxed">
            {data.accounting_recheck.status === "queued" && data.accounting_recheck.run_id ? (
              <>Order, tax and refund reconciliation was queued. <a className="text-primary underline" href={`/transaction-operations/runs/${encodeURIComponent(data.accounting_recheck.run_id)}`}>View reconciliation result →</a></>
            ) : "The full case recheck could not be queued. The case still needs review."}
          </p>
        )}
      </div>
      {!readOnly && pending && (
        <div className="space-y-4 border-t bg-muted/20 p-5 sm:px-6">
          <label className="flex items-start gap-2 text-xs leading-relaxed"><input type="checkbox" className="mt-0.5" checked={reviewed} disabled={disabled || blocked} onChange={e => setReviewed(e.target.checked)} />I reviewed the credit, its accounting impact and the invoice application.</label>
          <div className="flex flex-wrap justify-end gap-2">
            <button className="rounded-lg border px-4 py-2 text-xs font-medium disabled:opacity-50" disabled={disabled} onClick={onReject}>Reject</button>
            <button className="rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-primary-foreground disabled:opacity-50" disabled={disabled || blocked || !reviewed} onClick={onConfirm}>Approve {creditAmount(p.expected_after.credit_total, currency)} credit and application</button>
          </div>
        </div>
      )}
    </article>
  );
}
