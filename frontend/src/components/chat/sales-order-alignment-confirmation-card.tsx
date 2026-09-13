"use client";

import { useState } from "react";
import type { SalesOrderAlignmentReview, WriteConfirmationData } from "@/lib/types";
import { creditAmount } from "./sales-credit-confirmation-card";

function label(value: unknown) {
  if (value && typeof value === "object") {
    const ref = value as Record<string, unknown>;
    return String(ref.refName ?? ref.id ?? "Not verified");
  }
  return value == null ? "Not verified" : String(value);
}

export function SalesOrderAlignmentConfirmationCard({ data, proposal: p, onConfirm, onReject, disabled = false, readOnly = false, groupState }: {
  data: WriteConfirmationData; proposal: SalesOrderAlignmentReview;
  onConfirm: () => void; onReject: () => void; disabled?: boolean; readOnly?: boolean;
  groupState?: WriteConfirmationData["status"];
}) {
  const [acknowledged, setAcknowledged] = useState(false);
  const awaitingGroup = data.status === "pending" && groupState && groupState !== "pending";
  const pending = data.status === "pending" && !awaitingGroup;
  const verified = data.status === "approved" && data.accounting_verification?.status === "verified";
  const blocked = Boolean(data.invariant_errors?.length || data.unfillable_line_fields?.length || data.editable_slots?.length);
  const currency = p.profile.currency;
  const after = verified ? data.accounting_verification?.sales_order : p.expected_after;
  const state = awaitingGroup ? groupState === "executing" ? "Awaiting result" : "Not submitted"
    : verified ? "Executed · verified" : {
      pending: blocked ? "Needs review" : "Awaiting approval", executing: "Executing · checking results",
      approved: "Executed · verification needed", rejected: "Rejected", failed: "Needs review", indeterminate: "Outcome unconfirmed",
    }[data.status];
  return (
    <article className="overflow-hidden rounded-2xl border bg-card text-[13px]" aria-label={`Sales order source alignment ${p.order_reference}`}>
      <div className="space-y-4 p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-xl font-semibold tracking-tight">{verified ? "Sales order amendment verified" : "Align sales order with source"}</h3>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              Order {p.order_reference} (#{p.record_id}) · Linked invoice #{p.invoice_id}<br />
              {label(p.before.subsidiary)} · {currency} · NetSuite {data.target_environment?.toLowerCase() || "environment unverified"} {p.scope.netsuite_account_id}
            </p>
          </div>
          <span role="status" className="rounded-md bg-muted px-2.5 py-1 text-xs font-medium">{state}</span>
        </div>
        <p className="rounded-lg bg-muted/40 p-4 leading-relaxed">
          {verified ? "The sales order amendment was verified. Its linked invoice, ledger, billing and fulfillment evidence remain unchanged."
            : <>The linked invoice already includes the finalized <strong>{p.profile.source_adjustment_label}</strong> adjustment. Apply the same source-backed discount to the sales order to restore consistency.</>}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full tabular-nums" aria-label="Sales order amendment">
            <thead><tr className="border-b text-xs text-muted-foreground"><th className="py-3 text-left font-medium">Sales order · {currency}</th><th className="text-right font-medium">Before</th><th className="text-right font-medium">{verified ? "Verified after" : "Proposed"}</th></tr></thead>
            <tbody>{[
              ["Sales Adjustment", "discountTotal"], ["Sales order total", "total"],
              ["VAT / tax", "taxTotal"],
            ].map(([name, key]) => <tr key={key} className="border-b"><th className="py-3 pr-4 text-left font-medium">{name}</th><td className="whitespace-nowrap py-3 text-right text-muted-foreground">{creditAmount(p.before[key], currency)}</td><td className="whitespace-nowrap py-3 text-right font-semibold">{creditAmount(after?.[key as keyof typeof after], currency)}</td></tr>)}</tbody>
          </table>
        </div>
        <p className="text-xs text-muted-foreground">Source order total: {creditAmount(p.source.total, currency)}. Linked invoice total: {creditAmount(p.support.invoice.total, currency)} · unchanged.</p>
        {pending && <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 text-xs leading-relaxed">
          <strong className="block text-sm">Before you approve</strong>
          Approval authorizes only the displayed sales-order discount. Fresh checks must confirm the source, invoice and order still agree with this evidence. Existing item lines, billed and fulfilled quantities, classifications and original date are retained.
        </div>}
        {(data.error || (data.status !== "pending" && !verified && data.status !== "rejected" && data.status !== "executing")) && <p role="alert" className="rounded-lg bg-muted p-4">The outcome needs review. Check the recorded result before attempting another update. {data.error}</p>}
        {blocked && <p role="alert" className="rounded-lg bg-muted p-4">The accounting checks need review before approval.</p>}
        {verified && data.accounting_recheck?.status === "queued" && <p className="text-xs">Full reconciliation was queued. <a className="underline" href={`/transaction-operations/runs/${data.accounting_recheck.run_id}`}>View reconciliation result →</a></p>}
        {verified && data.accounting_recheck?.status === "not_queued" && <p className="text-xs">The full reconciliation could not be queued. The case still needs review.</p>}
        <details className="border-t pt-3"><summary className="cursor-pointer font-medium">Scope and verification</summary>
          <dl className="mt-3 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-[auto_1fr]">
            <dt>Updated record</dt><dd>Sales order #{p.record_id}</dd>
            <dt>Invoice retained</dt><dd>#{p.invoice_id} · {creditAmount(p.support.invoice.total, currency)}</dd>
            <dt>Discount item</dt><dd>Sales Adjustments · {p.profile.item_id}</dd>
            <dt>Ledger impact</dt><dd>No new posting; verify existing invoice GL is unchanged.</dd>
          </dl><p className="mt-3 text-xs leading-relaxed text-muted-foreground">{p.approval_basis}</p>
        </details>
        <p className="text-xs leading-relaxed text-muted-foreground">No new invoice, credit memo, rebilling or cash movement is authorized. Approval, approver identity, before/after evidence and verification are recorded in the audit log. Full order, tax and refund reconciliation determines whether the case is resolved; cash settlement remains separate.</p>
      </div>
      {!readOnly && pending && <div className="space-y-3 border-t bg-muted/20 px-5 py-4 sm:px-6">
        <label className="flex items-start gap-2 text-xs leading-relaxed"><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} disabled={disabled || blocked} className="mt-0.5" />I approve the displayed sales-order amendment.</label>
        <div className="flex gap-2"><button onClick={onConfirm} disabled={disabled || blocked || !acknowledged} className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">Approve sales-order amendment</button><button onClick={onReject} disabled={disabled} className="rounded-md border px-4 py-2 text-sm">Reject</button></div>
      </div>}
    </article>
  );
}
