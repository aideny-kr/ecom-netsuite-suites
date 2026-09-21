"use client";

import { useState } from "react";
import type { NativeAccountingReview, WriteConfirmationData } from "@/lib/types";
import { creditAmount } from "./sales-credit-confirmation-card";

export function NativeAccountingConfirmationCard({ data, proposal: p, onConfirm, onReject, disabled = false, readOnly = false, groupState }: {
  data: WriteConfirmationData; proposal: NativeAccountingReview;
  onConfirm: () => void; onReject: () => void; disabled?: boolean; readOnly?: boolean;
  groupState?: WriteConfirmationData["status"];
}) {
  const [acknowledged, setAcknowledged] = useState(false);
  const credit = p.kind === "credit_tax_reallocation";
  const verified = data.status === "approved" && data.accounting_verification?.status === "verified";
  const waitingGroup = data.status === "pending" && groupState && groupState !== "pending";
  const pending = data.status === "pending" && !waitingGroup;
  const targetVerified = data.target_account?.toLowerCase().replace(/_/g, "-") === p.scope.netsuite_account_id.toLowerCase().replace(/_/g, "-") && ["PRODUCTION", "SANDBOX"].includes(data.target_environment || "");
  const blocked = !targetVerified || Boolean(data.invariant_errors?.length || data.unfillable_line_fields?.length || data.editable_slots?.length);
  const currency = typeof p.source.currency === "string" ? p.source.currency : "Currency unknown";
  const status = waitingGroup ? groupState === "executing" ? "Awaiting result" : "Not submitted" : verified ? "Executed · verified" : {
    pending: blocked ? "Needs review" : "Awaiting approval", executing: "Executing · checking results",
    approved: "Executed · verification needed", rejected: "Rejected", failed: "Needs review", indeterminate: "Outcome unconfirmed",
  }[data.status];
  const after: Record<string, unknown> | undefined = verified ? data.accounting_verification?.after?.body : p.expected_after;
  return (
    <article className="overflow-hidden rounded-2xl border bg-card text-[13px]" aria-label={`Accounting amendment ${p.order_reference}`}>
      <div className="space-y-4 p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-xl font-semibold tracking-tight">{credit ? "Correct existing credit tax allocation" : "Align sales-order lines and tax"}</h3>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              Order {p.order_reference} · {credit ? "Credit memo" : "Sales order"} #{p.record_id} · Invoice #{p.invoice_id}<br />
              {currency} · NetSuite {data.target_environment || "environment unverified"} · {p.scope.netsuite_account_id}
            </p>
          </div>
          <span role="status" className="rounded-md bg-muted px-2.5 py-1 text-xs font-medium">{status}</span>
        </div>
        <p className="rounded-lg bg-muted/40 p-4 leading-relaxed">
          {verified ? "Fresh reads verified this amendment and its related posting records. Full order reconciliation and settlement are evaluated separately."
            : credit ? "Reallocate the existing credit between sales adjustments and tax. Keep the gross credit, refund, applications and paid invoice unchanged."
              : "The related credit correction has been verified. Align the identified sales-order prices and integration VAT with the source, preserving quantities, fulfillment, billing and posting records."}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full tabular-nums" aria-label="Financial impact">
            <thead><tr className="border-b text-xs text-muted-foreground"><th className="py-3 text-left font-medium">Financial impact</th><th className="px-2 text-right font-medium">Before</th><th className="text-right font-medium">{verified ? "Verified after" : "Proposed"}</th></tr></thead>
            <tbody>{[[credit ? "Sales adjustments" : "Item subtotal", "subtotal"], ["Tax / VAT", "taxTotal"], [credit ? "Gross credit" : "Order total", "total"]].map(([label, key]) => (
              <tr key={key} className="border-b"><th className="py-3 text-left font-medium">{label}</th><td className="whitespace-nowrap px-2 text-right text-muted-foreground">{creditAmount(p.before[key], currency)}</td><td className="whitespace-nowrap text-right font-semibold">{creditAmount(after?.[verified && key === "taxTotal" ? "taxtotal" : key], currency)}</td></tr>
            ))}</tbody>
          </table>
          <p className="mt-2 text-xs text-muted-foreground">The total includes tax. {credit ? "No new credit or cash refund is created." : "The sales order is non-posting; its linked posting records are checked again."}</p>
        </div>
        {credit && p.refund_allocation && <section className="space-y-3 rounded-lg border p-4" aria-label="Refund audit evidence">
          <div>
            <h4 className="font-semibold">Refund allocation · Finance review required</h4>
            <p className="mt-1 text-xs text-muted-foreground">Solidus refund #{p.refund_allocation.source_refund_id} · Payment {p.refund_allocation.payment_number}</p>
          </div>
          <p className="text-xs leading-relaxed text-muted-foreground">{p.refund_allocation.authority}</p>
          <div className="overflow-x-auto">
            <table className="w-full tabular-nums" aria-label="Refunded lines">
              <thead><tr className="border-b text-xs text-muted-foreground"><th className="py-2 text-left font-medium">Source line / SKU</th><th className="px-2 text-right font-medium">Qty</th><th className="px-2 text-right font-medium">Net reduction</th><th className="px-2 text-right font-medium">Tax reduction</th><th className="text-right font-medium">Gross refund</th></tr></thead>
              <tbody>{p.refund_allocation.lines.map(line => <tr className="border-b" key={line.source_line_id}>
                <th className="py-3 text-left font-medium">{line.sku}<span className="block text-xs font-normal text-muted-foreground">Line #{line.source_line_id}</span></th>
                <td className="px-2 text-right">{line.quantity}</td>
                <td className="whitespace-nowrap px-2 text-right">{creditAmount(line.net, currency)}</td>
                <td className="whitespace-nowrap px-2 text-right">{creditAmount(line.tax, currency)}</td>
                <td className="whitespace-nowrap text-right font-semibold">{creditAmount(line.gross, currency)}</td>
              </tr>)}</tbody>
              <tfoot><tr className="font-semibold"><th colSpan={2} className="pt-3 text-left">Total</th><td className="whitespace-nowrap px-2 pt-3 text-right">{creditAmount(p.refund_allocation.net, currency)}</td><td className="whitespace-nowrap px-2 pt-3 text-right">{creditAmount(p.refund_allocation.tax, currency)}</td><td className="whitespace-nowrap pt-3 text-right">{creditAmount(p.refund_allocation.gross, currency)}</td></tr></tfoot>
            </table>
          </div>
          <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">Source amounts and audit versions</summary>
            <p className="mt-2">Order version #{p.refund_allocation.order_version_id}. Amounts are calculated from recorded changes; no model estimated the tax.</p>
            <ul className="mt-2 space-y-2">{p.refund_allocation.lines.map(line => <li key={line.source_line_id}>Line #{line.source_line_id}: unit price {creditAmount(line.price_before, currency)} → {creditAmount(line.price_after, currency)}; line tax {creditAmount(line.tax_before, currency)} → {creditAmount(line.tax_after, currency)}. Versions {line.version_ids.join(", ")}.</li>)}</ul>
          </details>
        </section>}
        <dl className="grid gap-3 rounded-lg border p-4 sm:grid-cols-2">
          <div><dt className="text-xs text-muted-foreground">AR account</dt><dd className="mt-1">{p.ar_account}</dd></div>
          <div><dt className="text-xs text-muted-foreground">Adjustment / tax accounts</dt><dd className="mt-1">{p.sales_adjustment_account} / {p.tax_account}</dd></div>
          <div><dt className="text-xs text-muted-foreground">Accounting book</dt><dd className="mt-1">{p.accounting_book}</dd></div>
          <div><dt className="text-xs text-muted-foreground">Posting period retained</dt><dd className="mt-1">#{String(p.period.id || "Not verified")} · {credit ? "Open period required" : "Non-posting amendment"}</dd></div>
        </dl>
        <details className="rounded-lg border p-4"><summary className="cursor-pointer font-medium">Accounting basis and exact changes</summary>
          <p className="mt-3 leading-relaxed text-muted-foreground">{p.approval_basis}</p>
          <pre className="mt-3 overflow-x-auto whitespace-pre-wrap break-words rounded bg-muted/40 p-3 text-xs">{JSON.stringify(p.proposed_fields, null, 2)}</pre>
        </details>
        {pending && !readOnly && <label className="flex items-start gap-3 leading-relaxed"><input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} disabled={disabled || blocked} className="mt-0.5" /><span>{p.refund_allocation ? "I confirm these line changes belong to this refund and approve the tax basis, accounts and exact changes." : "I reviewed the source reduction, tax basis, accounts and exact changes."}</span></label>}
        {blocked && <p className="text-sm">The account or correction evidence needs review before approval.</p>}
        {pending && !readOnly && <div className="flex flex-wrap justify-end gap-3"><button onClick={onReject} disabled={disabled} className="rounded-lg border px-4 py-2 disabled:opacity-50">Reject</button><button onClick={onConfirm} disabled={disabled || blocked || !acknowledged} className="rounded-lg bg-primary px-4 py-2 font-medium text-primary-foreground disabled:opacity-50">Approve correction</button></div>}
      </div>
    </article>
  );
}
