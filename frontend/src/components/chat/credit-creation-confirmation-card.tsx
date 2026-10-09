"use client";

import { useState } from "react";
import type { CreditCreationReview, WriteConfirmationData } from "@/lib/types";
import { creditAmount } from "./sales-credit-confirmation-card";

/** A new credit the assistant proposed and the server accepted by outcome: the order's posted
 * balance with this credit equals the finalized source (smart resolver, slice 1). */
export function CreditCreationConfirmationCard({
  data, proposal: p, onConfirm, onReject, disabled = false, readOnly = false, groupState,
}: {
  data: WriteConfirmationData;
  proposal: CreditCreationReview;
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
  const currency = typeof p.source.currency === "string" ? p.source.currency : "Currency unknown";
  const total = p.expected_after.total;
  const state = awaitingGroup
    ? groupState === "executing" ? "Awaiting result" : "Not submitted"
    : verified ? "Executed · verified" : {
      pending: blocked ? "Needs review" : "Awaiting approval",
      executing: "Executing · checking results",
      approved: "Executed · verification needed",
      rejected: "Rejected", failed: "Needs review", indeterminate: "Outcome unconfirmed",
    }[data.status];
  const creditNumber = data.accounting_verification?.resolution?.credit_memo_number
    ?? (data.accounting_verification as { credit_memo_number?: string } | null | undefined)?.credit_memo_number;
  const rows: Array<[string, { gross: string; tax: string }]> = [
    ["NetSuite now (invoices less credits)", p.balance.before],
    ["After this credit", p.balance.after],
    ["Finalized source", p.balance.source],
  ];
  return (
    <article className="overflow-hidden rounded-2xl border bg-card text-[13px]" aria-label={`New credit ${p.order_reference}`}>
      <div className="space-y-4 p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-xl font-semibold tracking-tight">{verified ? `Credit ${creditNumber ?? ""} applied`.trim() : "Create and apply a credit"}</h3>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              Order {p.order_reference} · Invoice {String(p.before.tranId ?? p.invoice_id)} (#{p.invoice_id})<br />
              {currency} · NetSuite {data.target_environment?.toLowerCase() || "environment unverified"} {p.scope.netsuite_account_id}
            </p>
          </div>
          <span role="status" className="rounded-md bg-muted px-2.5 py-1 text-xs font-medium">{state}</span>
        </div>
        <p className="rounded-lg bg-muted/40 p-4 leading-relaxed">
          {verified
            ? "The credit, its lines, GL and invoice application were read back, and the order now equals the finalized source."
            : <>Memo <strong>{p.memo}</strong>. The server checked that this credit makes the order equal the finalized source; the invoice, its existing credits and the sales order are not changed.</>}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full tabular-nums" aria-label="Order balance">
            <thead><tr className="border-b text-xs text-muted-foreground"><th className="py-3 text-left font-medium">Order balance · {currency}</th><th className="py-3 text-right font-medium">Total</th><th className="py-3 text-right font-medium">Tax</th></tr></thead>
            <tbody>{rows.map(([name, value]) => (
              <tr key={name} className="border-b">
                <th className="py-3 pr-4 text-left font-medium">{name}</th>
                <td className="whitespace-nowrap py-3 text-right">{creditAmount(value.gross, currency)}</td>
                <td className="whitespace-nowrap py-3 text-right">{creditAmount(value.tax, currency)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        {!pending && !verified && (
          <p role="status" className="rounded-lg border p-3 leading-relaxed">
            {data.status === "executing" ? "The approved credit is in progress. Wait for independent verification."
              : data.status === "rejected" ? "This proposal was rejected."
                : "The credit is not verified. Review the recorded outcome before attempting another write."}
            {data.error && ` ${data.error}`}{data.accounting_verification?.reason && ` ${data.accounting_verification.reason}`}
          </p>
        )}
        {blocked && (
          <div role="alert" className="rounded-lg border border-amber-500/30 p-3">
            {[...(data.invariant_errors || []), ...(data.unfillable_line_fields || [])].map(error => <p key={error}>{error}</p>)}
          </div>
        )}
        <details className="border-y py-3">
          <summary className="cursor-pointer font-medium">Lines, accounting impact and verification</summary>
          <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-2 text-xs sm:grid-cols-[auto_1fr]">
            {p.lines.map((line, index) => (
              <div key={`${line.item_id}-${index}`} className="contents"><dt>Line {index + 1}</dt><dd>Item {line.item_id} · {creditAmount(line.amount, currency)} · Non-taxable</dd></div>
            ))}
            {Object.entries(p.expected_ledger.debit).map(([account, amount]) => (
              <div key={`d-${account}`} className="contents"><dt>Debit</dt><dd>Account {account} · {creditAmount(amount, currency)}</dd></div>
            ))}
            {Object.entries(p.expected_ledger.credit).map(([account, amount]) => (
              <div key={`c-${account}`} className="contents"><dt>Credit</dt><dd>Accounts Receivable {account} · {creditAmount(amount, currency)}</dd></div>
            ))}
            <dt>Apply only to</dt><dd>Invoice {String(p.before.tranId ?? p.invoice_id)} · {creditAmount(total, currency)}</dd>
            <dt>Posting date / period</dt><dd>{String(p.proposed_fields.tranDate ?? "")} · {String(p.period.periodName ?? p.period.id ?? "")}</dd>
          </dl>
          <p className="mt-3 text-xs leading-relaxed text-muted-foreground">{p.approval_basis}</p>
          <details className="mt-3"><summary className="cursor-pointer text-xs">Exact credit fields</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-muted/40 p-3 text-xs">{JSON.stringify(p.proposed_fields, null, 2)}</pre></details>
        </details>
        {verified && data.accounting_recheck && (
          <p className="text-xs leading-relaxed">
            {data.accounting_recheck.run_id ? (
              <>Order reconciliation was rechecked. <a className="text-primary underline" href={`/transaction-operations/runs/${encodeURIComponent(data.accounting_recheck.run_id)}`}>View reconciliation result →</a></>
            ) : "The case recheck could not be queued. The case still needs review."}
          </p>
        )}
      </div>
      {!readOnly && pending && (
        <div className="space-y-4 border-t bg-muted/20 p-5 sm:px-6">
          <label className="flex items-start gap-2 text-xs leading-relaxed"><input type="checkbox" className="mt-0.5" checked={reviewed} disabled={disabled || blocked} onChange={e => setReviewed(e.target.checked)} />I reviewed the credit, its accounting impact and the invoice application.</label>
          <div className="flex flex-wrap justify-end gap-2">
            <button className="rounded-lg border px-4 py-2 text-xs font-medium disabled:opacity-50" disabled={disabled} onClick={onReject}>Reject</button>
            <button className="rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-primary-foreground disabled:opacity-50" disabled={disabled || blocked || !reviewed} onClick={onConfirm}>Approve {creditAmount(total, currency)} credit and application</button>
          </div>
        </div>
      )}
    </article>
  );
}
