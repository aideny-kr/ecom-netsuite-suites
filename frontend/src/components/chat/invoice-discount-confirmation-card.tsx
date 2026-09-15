"use client";

import { useState } from "react";
import type { InvoiceDiscountReview, WriteConfirmationData } from "@/lib/types";
import { creditAmount } from "./sales-credit-confirmation-card";

function label(value: unknown) {
  if (value && typeof value === "object") {
    const ref = value as Record<string, unknown>;
    return String(ref.refName ?? ref.id ?? "Not verified");
  }
  return value == null ? "Not verified" : String(value);
}

export function InvoiceDiscountConfirmationCard({ data, proposal: p, onConfirm, onReject, disabled = false, readOnly = false, groupState }: {
  data: WriteConfirmationData; proposal: InvoiceDiscountReview;
  onConfirm: () => void; onReject: () => void; disabled?: boolean; readOnly?: boolean;
  groupState?: WriteConfirmationData["status"];
}) {
  const [acknowledged, setAcknowledged] = useState(false);
  const awaitingGroup = data.status === "pending" && groupState && groupState !== "pending";
  const pending = data.status === "pending" && !awaitingGroup;
  const verified = data.status === "approved" && data.accounting_verification?.status === "verified";
  const blocked = Boolean(data.invariant_errors?.length || data.unfillable_line_fields?.length || data.editable_slots?.length);
  const currency = p.profile.currency;
  const after = verified ? data.accounting_verification?.invoice : p.expected_after;
  const state = awaitingGroup ? groupState === "executing" ? "Awaiting result" : "Not submitted"
    : verified ? "Executed · verified" : {
      pending: blocked ? "Needs review" : "Awaiting approval", executing: "Executing · checking results",
      approved: "Executed · verification needed", rejected: "Rejected", failed: "Needs review", indeterminate: "Outcome unconfirmed",
    }[data.status];
  return (
    <article className="overflow-hidden rounded-2xl border bg-card text-[13px]" aria-label={`Invoice Sales Adjustment ${p.order_reference}`}>
      <div className="space-y-4 p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-xl font-semibold tracking-tight">{verified ? "Invoice discount verified" : "Apply Sales Adjustment to unpaid invoice"}</h3>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              Order {p.order_reference} · Invoice {label(p.before.tranId)} (#{p.record_id})<br />
              {label(p.before.subsidiary)} · {currency} · NetSuite {data.target_environment?.toLowerCase() || "environment unverified"} {p.scope.netsuite_account_id}
            </p>
          </div>
          <span role="status" className="rounded-md bg-muted px-2.5 py-1 text-xs font-medium">{state}</span>
        </div>
        <p className="rounded-lg bg-muted/40 p-4 leading-relaxed">
          {verified ? "The approved invoice discount and its general ledger impact were independently re-read and verified."
            : <>This invoice is fully unpaid with no payment or credit applications. Apply the finalized <strong>{p.profile.source_adjustment_label}</strong> adjustment directly to the invoice using Sales Adjustments.</>}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full tabular-nums" aria-label="Invoice discount impact">
            <thead><tr className="border-b text-xs text-muted-foreground"><th className="py-3 text-left font-medium">Financial impact · {currency}</th><th className="text-right font-medium">Before</th><th className="text-right font-medium">{verified ? "Verified after" : "Proposed"}</th></tr></thead>
            <tbody>{[
              ["Sales Adjustment", "discountTotal"], ["Invoice total", "total"],
              ["VAT / tax", "taxTotal"], ["Amount paid", "amountPaid"], ["Remaining receivable", "amountRemaining"],
            ].map(([name, key]) => <tr key={key} className="border-b"><th className="py-3 pr-4 text-left font-medium">{name}</th><td className="whitespace-nowrap py-3 text-right text-muted-foreground">{creditAmount(p.before[key], currency)}</td><td className="whitespace-nowrap py-3 text-right font-semibold">{creditAmount(after?.[key as keyof typeof after], currency)}</td></tr>)}</tbody>
          </table>
        </div>
        <p className="text-xs text-muted-foreground">Source order total: {creditAmount(p.source.total, currency)}. Original invoice items and posting period are retained.</p>
        {pending && <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 text-xs leading-relaxed">
          <strong className="block text-sm">Before you approve</strong>
          Approval authorizes this discount on the issued invoice. The agent checks that it is still fully unpaid, that no discount or credit was added, and that its posting period remains open. Tax is zero in the source and invoice; no new exemption is being established.
        </div>}
        {(data.error || (data.status !== "pending" && !verified && data.status !== "rejected" && data.status !== "executing")) && <p role="alert" className="rounded-lg bg-muted p-4">The outcome needs review. Check the recorded result before attempting another update. {data.error}</p>}
        {blocked && <p role="alert" className="rounded-lg bg-muted p-4">The accounting checks need review before approval.</p>}
        {verified && data.accounting_recheck?.status === "queued" && <p className="text-xs">Full reconciliation was queued. <a className="underline" href={`/transaction-operations/runs/${data.accounting_recheck.run_id}`}>View reconciliation result →</a></p>}
        {verified && data.accounting_recheck?.status === "not_queued" && <p className="text-xs">The full reconciliation could not be queued. The case still needs review.</p>}
        <details className="border-t pt-3"><summary className="cursor-pointer font-medium">Accounting impact and verification</summary>
          <dl className="mt-3 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-[auto_1fr]">
            <dt>Debit</dt><dd>Sales Adjustments · {p.sales_adjustment_account}</dd>
            <dt>Reduce receivables</dt><dd>{p.ar_account_name || p.ar_account}</dd>
            <dt>Discount item</dt><dd>Sales Adjustments · {p.profile.item_id}</dd>
            <dt>Posting period / book</dt><dd>{label(p.before.postingPeriod)} · Book {p.accounting_book}</dd>
          </dl><p className="mt-3 text-xs leading-relaxed text-muted-foreground">{p.approval_basis}</p>
        </details>
        <p className="text-xs leading-relaxed text-muted-foreground">No credit memo or cash refund is created. Approval, approver identity, before/after evidence and verification are recorded in the audit log. Full order, tax and refund reconciliation determines whether the case is resolved; cash settlement remains separate.</p>
      </div>
      {!readOnly && pending && <div className="space-y-3 border-t bg-muted/20 px-5 py-4 sm:px-6">
        <label className="flex items-start gap-2 text-xs leading-relaxed"><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} disabled={disabled || blocked} className="mt-0.5" />I approve the displayed Sales Adjustment on this unpaid invoice.</label>
        <div className="flex gap-2"><button onClick={onConfirm} disabled={disabled || blocked || !acknowledged} className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">Approve invoice adjustment</button><button onClick={onReject} disabled={disabled} className="rounded-md border px-4 py-2 text-sm">Reject</button></div>
      </div>}
    </article>
  );
}
