"use client";

import { useState } from "react";
import { groupProgress, LIVE_LABEL, liveState, type LiveState } from "@/lib/accounting-group-progress";
import type { WriteConfirmationData } from "@/lib/types";
import { AccountingConfirmationCard } from "./accounting-confirmation-card";
import { creditAmount } from "./sales-credit-confirmation-card";
import { AccountingOrderPlanCard } from "./accounting-order-plan-card";

const WRITTEN = "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300";
const STOPPED = "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300";
const PILL: Record<LiveState, string> = {
  reconciled: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
  further_review: WRITTEN,
  rechecking: WRITTEN,
  writing: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  queued: "bg-muted text-muted-foreground",
  refused: STOPPED,
  needs_review: STOPPED,
};

function LivePill({ state }: { state: LiveState }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-semibold ${PILL[state]}`}>
      {state === "writing" && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current motion-reduce:animate-none" aria-hidden />}
      {LIVE_LABEL[state]}
    </span>
  );
}

export function AccountingGroupCard({
  data,
  onConfirm,
  onReject,
  disabled,
}: {
  data: WriteConfirmationData;
  onConfirm: () => void;
  onReject: () => void;
  disabled?: boolean;
}) {
  const [reviewed, setReviewed] = useState(false);
  const group = data.accounting_group!;
  const eligible = group.members.filter((m) => m.card);
  const hasRefundAllocation = eligible.some((member) => {
    const review = member.card?.accounting_review;
    return review?.kind === "credit_tax_reallocation" && Boolean(review.refund_allocation);
  });
  const verified = eligible.filter(
    (m) =>
      m.card?.status === "approved" &&
      m.card.accounting_verification?.status === "verified",
  ).length;
  const pending = data.status === "pending";
  const progress = data.accounting_plan_progress;
  const dispatch = data.accounting_group_dispatch;
  // Live: each order's receipt when it has one, else the dispatch record (refreshed every 5 s).
  const live = dispatch ? groupProgress(group.members, dispatch) : null;
  const allReconciled =
    eligible.length > 0 && eligible.every((m) => liveState(m, dispatch) === "reconciled");
  const blocked = Boolean(
    data.invariant_errors?.length || data.unfillable_line_fields?.length,
  );
  return (
    <section
      className="overflow-hidden rounded-2xl border border-amber-500/40 bg-card"
      aria-label="Group correction approval"
    >
      <div className="space-y-4 p-5 sm:p-6">
        <div className="flex flex-wrap justify-between gap-3">
          <div>
            <h3 className="text-xl font-semibold">
              {pending && eligible.length > 0
                ? "Review group corrections"
                : allReconciled
                  ? `${eligible.length} of ${eligible.length} corrections reconciled`
                  : "Group correction results"}
            </h3>
            <p className="mt-1 text-xs text-muted-foreground">
              {group.members.length} orders reviewed · {eligible.length} exact
              corrections · {group.members.length - eligible.length} need
              further investigation
            </p>
          </div>
          <span role="status" className="text-xs font-medium">
            {pending
              ? blocked || !eligible.length
                ? "Needs review"
                : "Awaiting approval"
              : data.status === "executing" && live
                ? `${live.done} of ${live.total} done`
                : allReconciled
                  ? "Group reconciled"
                  : data.status === "executing"
                ? "Running · awaiting results"
                : data.status === "rejected"
                  ? "Rejected"
                  : data.status === "indeterminate"
                    ? "Incomplete · review recorded outcomes"
                    : `${verified} / ${eligible.length} verified`}
          </span>
        </div>
        {live && live.total > 0 && (
          <div className="space-y-2" aria-label="Live correction progress">
            <div className="flex h-2.5 overflow-hidden rounded-full bg-muted" role="img"
              aria-label={`${live.counts.reconciled} reconciled, ${live.counts.rechecking + live.counts.further_review} written, ${live.counts.writing} writing, ${live.counts.queued} queued`}>
              <span className="h-full bg-emerald-600" style={{ width: `${(live.counts.reconciled / live.total) * 100}%` }} />
              <span className="h-full bg-sky-600" style={{ width: `${((live.counts.rechecking + live.counts.further_review) / live.total) * 100}%` }} />
              <span className="h-full bg-amber-500" style={{ width: `${(live.counts.writing / live.total) * 100}%` }} />
              <span className="h-full bg-red-600" style={{ width: `${((live.counts.refused + live.counts.needs_review) / live.total) * 100}%` }} />
            </div>
            <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span><strong className="tabular-nums text-foreground">{live.counts.reconciled}</strong> reconciled</span>
              <span><strong className="tabular-nums text-foreground">{live.counts.rechecking + live.counts.further_review}</strong> written · rechecking</span>
              <span><strong className="tabular-nums text-foreground">{live.counts.writing}</strong> writing</span>
              <span><strong className="tabular-nums text-foreground">{live.counts.queued}</strong> queued</span>
              {live.counts.refused + live.counts.needs_review > 0 && (
                <span><strong className="tabular-nums text-foreground">{live.counts.refused + live.counts.needs_review}</strong> stopped or need review</span>
              )}
            </p>
            {live.writing.length > 0 && (
              <p aria-label="Writing now" className="rounded-lg border bg-muted/30 px-3 py-2 text-[13px]">
                <strong>Writing now</strong>{" "}
                <span className="font-mono tabular-nums">{live.writing.join(" · ")}</span>
              </p>
            )}
            {(dispatch?.status === "queued" || dispatch?.status === "running") && (
              <p className="text-xs text-muted-foreground">
                Each fix is read back from NetSuite, then the order is rechecked against the source. You can leave this page.
              </p>
            )}
          </div>
        )}
        {progress && progress.results_ready > 0 && (
          <p role="status" className="text-[13px] font-medium">
            {progress.prepared === undefined
              ? `${progress.reconciled} / ${progress.orders} orders reconciled · ${progress.remaining} need further review`
              : `${progress.reconciled} / ${progress.prepared} approved corrections reconciled · ${progress.remaining} still to reconcile` +
                (progress.unprepared ? ` · ${progress.unprepared} of ${progress.orders} orders not prepared` : "")}
          </p>
        )}
        <p className="text-[13px] leading-relaxed text-muted-foreground">
          {eligible.length > 0 ? (
            <>
              Each correction has its own verified source evidence and exact
              accounting treatment shown below. After approval, up
              to {group.concurrency} corrections run simultaneously. Each
              invoice is checked again before writing and independently verified
              afterward.
            </>
          ) : (
            "No validated accounting changes are ready. Continue the shared investigations below before preparing exact corrections for approval."
          )}
        </p>
        {Boolean(group.treatment_batches?.length) && (
          <div className="space-y-2 rounded-lg border p-3" aria-label="Validated accounting treatments">
            <h4 className="text-sm font-semibold">Accounting treatments</h4>
            {group.treatment_batches!.map((batch) => (
              <p key={batch.treatment_id} className="text-xs leading-relaxed">
                <strong>{batch.label}</strong> · {batch.case_ids.length} orders · {batch.treatment.currency || "Currency unverified"}
                <span className="block text-muted-foreground">
                  Book {batch.treatment.accounting_book} · AR account {batch.treatment.ar_account} · Offset account {batch.treatment.offset_account || "Unverified"}
                </span>
              </p>
            ))}
            <p className="text-xs text-muted-foreground">Exact changes and remaining receivables are shown per order below. Cash settlement requires separate verification.</p>
          </div>
        )}
        {Boolean(group.investigation_batches?.length) && (
          <div className="space-y-2 rounded-lg border p-3" aria-label="Shared investigations">
            <h4 className="text-sm font-semibold">Shared investigations</h4>
            <p className="text-xs text-muted-foreground">These steps do not post changes. An order may need more than one check.</p>
            {group.investigation_batches!.map((batch) => (
              <p key={batch.code} className="text-xs leading-relaxed">
                <strong>{batch.case_ids.length} orders</strong> · {batch.next_step}
              </p>
            ))}
          </div>
        )}
        {data.status === "indeterminate" && (
          <div
            role="alert"
            className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-4 text-[13px] leading-relaxed"
          >
            <strong>Group processing is incomplete.</strong> {verified} of{" "}
            {eligible.length} corrections are verified. Some changes may already
            have reached NetSuite. Review each recorded outcome before preparing
            another write. Queued work may have stopped; there is no automatic
            retry. A rejection is complete only for items marked Rejected.
          </div>
        )}
        {pending && eligible.length > 0 && (
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 text-xs leading-relaxed">
            <strong>Before you approve:</strong> Review each order’s exact
            amounts, accounts, tax treatment, application and posting period below.
            Approval confirms those accounting choices. A changed record is
            stopped; an unconfirmed outcome stops further queued writes. Orders
            already running may finish.
            {hasRefundAllocation && " For each audited refund, confirm that the displayed line changes belong to that refund. Audit history does not contain an explicit refund-to-line link."}
          </div>
        )}
        <div className="space-y-3">
          {group.members.map((member) => {
            const card = member.card;
            const p = card?.accounting_review;
            const receipt = member.resolution_receipt || card?.accounting_receipt;
            const originalReceipt = card?.accounting_receipt;
            const state = liveState(member, dispatch);
            if (card && (receipt?.plan || p?.resolution_plan)) {
              return <AccountingOrderPlanCard key={member.case_id} data={card} receipt={receipt} groupState={data.status}>
                {state && <p className="mb-2"><LivePill state={state} /></p>}
                {member.reason && <p className="mb-3 text-xs leading-relaxed">{member.reason}</p>}
                <AccountingConfirmationCard data={card} onConfirm={() => {}} onReject={() => {}} readOnly groupState={data.status} />
              </AccountingOrderPlanCard>;
            }
            const hasLaterReceipt = originalReceipt && receipt && originalReceipt.completion_audit_id !== receipt.completion_audit_id;
            const result =
              receipt?.status === "reconciled" ? "Reconciled" :
              receipt ? receipt.status === "partially_resolved" ? "Correction verified · further review" : "Verification needs review" :
              card?.accounting_verification?.status === "verified" &&
              card.status === "approved"
                ? "Verified"
                : card?.status === "indeterminate" ||
                    card?.status === "executing"
                  ? "Outcome unconfirmed"
                  : card?.status === "pending"
                    ? pending
                      ? "Ready for review"
                      : data.status === "executing"
                        ? "Awaiting result"
                        : "Not submitted"
                    : member.set_aside
                      ? `Set aside · ${member.set_aside}`
                      : member.reason
                        ? "Needs review"
                        : card?.status || "Needs investigation";
            return (
              <details key={member.case_id} className="rounded-lg border p-3">
                <summary className="cursor-pointer text-[13px]">
                  <span className="font-semibold">
                    {member.order_reference}
                  </span>
                  <span className="ml-3 text-xs text-muted-foreground">
                    {state ? <LivePill state={state} /> : result}
                  </span>
                  {p?.kind === "sales_adjustment_credit" ? (
                    <span className="mt-1 block text-xs text-muted-foreground">
                      {p.profile.currency} · Sales Adjustments credit {creditAmount(p.expected_after.credit_total, p.profile.currency)} · Net invoice {creditAmount(p.expected_after.net_invoice_total, p.profile.currency)} · Tax impact {creditAmount(p.expected_after.credit_tax, p.profile.currency)}
                    </span>
                  ) : (p?.kind === "invoice_sales_adjustment" || p?.kind === "sales_order_source_alignment") ? (
                    <span className="mt-1 block text-xs text-muted-foreground">
                      {p.profile.currency} · {p.kind === "sales_order_source_alignment" ? "Sales order discount" : "Invoice discount"} {creditAmount(p.expected_after.discountTotal, p.profile.currency)} · Total {creditAmount(p.before.total, p.profile.currency)} → {creditAmount(p.expected_after.total, p.profile.currency)}
                    </span>
                  ) : (p?.kind === "credit_tax_reallocation" || p?.kind === "sales_order_line_alignment") ? (
                    <span className="mt-1 block text-xs text-muted-foreground">
                      {p.kind === "credit_tax_reallocation" ? "Existing credit allocation" : "Sales-order alignment"} · Tax {creditAmount(p.before.taxTotal, String(p.source.currency || ""))} → {creditAmount(p.expected_after.taxTotal, String(p.source.currency || ""))} · Total {creditAmount(p.before.total, String(p.source.currency || ""))} → {creditAmount(p.expected_after.total, String(p.source.currency || ""))}
                    </span>
                  ) : p && (
                    <span className="mt-1 block text-xs text-muted-foreground">
                      {String(p.before.currency_code || "Currency unknown")} ·
                      Tax {Number(p.before.taxTotal).toFixed(2)} →{" "}
                      {Number(p.expected_after.taxTotal).toFixed(2)} · Total{" "}
                      {Number(p.before.total).toFixed(2)} →{" "}
                      {Number(p.expected_after.total).toFixed(2)} · Δ{" "}
                      {(
                        Number(p.expected_after.total) - Number(p.before.total)
                      ).toFixed(2)}
                    </span>
                  )}
                </summary>
                <div className="mt-3">
                  {receipt && (
                    <div className="mb-3 space-y-2 text-xs leading-relaxed" aria-label="Verified accounting result">
                      <p>{receipt.summary}</p>
                      {hasLaterReceipt && (
                        <div aria-label="Original correction approval">
                          <p>Original correction approved by {originalReceipt.approved_by.name} · {new Date(originalReceipt.approved_at).toLocaleString()}</p>
                          <p className="break-all text-muted-foreground">Original audit reference: {originalReceipt.completion_audit_id}</p>
                        </div>
                      )}
                      <p>{hasLaterReceipt ? "Latest correction approved by" : "Approved by"} {receipt.approved_by.name} · {new Date(receipt.approved_at).toLocaleString()}</p>
                      <p className="break-all text-muted-foreground">Audit reference: {receipt.completion_audit_id}</p>
                      <div className="flex flex-wrap gap-3">
                        {receipt.record_links.map((link) => (
                          <a key={`${link.record_type}:${link.record_id}`} href={link.url} target="_blank" rel="noopener noreferrer" className="text-primary underline">{link.label}</a>
                        ))}
                        <a href={receipt.reconciliation_url} className="text-primary underline">Reconciliation result</a>
                        <a href={receipt.audit_url} className="text-primary underline">Audit log</a>
                      </div>
                      {receipt.next_step.status === "awaiting_approval" && <p>The next correction requires approval in the new group card below.</p>}
                      {receipt.next_step.reasons?.map((reason) => <p key={reason}>{reason}</p>)}
                    </div>
                  )}
                  {member.reason && (
                    <p className="mb-3 text-xs leading-relaxed">
                      {member.reason}
                    </p>
                  )}
                  {!card && (
                    <a
                      className="text-xs text-primary underline"
                      href={`/chat?${new URLSearchParams({ compose: `Investigate case ${member.case_id} for order ${member.order_reference}. Review the saved case and accounting evidence, explain why the group could not prepare an exact correction, and fetch only missing evidence. Propose a supported fix for human approval; do not execute changes.`, new_session: "true" })}`}
                    >
                      Investigate this order →
                    </a>
                  )}
                  {card && (
                    <AccountingConfirmationCard
                      data={card}
                      onConfirm={() => {}}
                      onReject={() => {}}
                      readOnly
                      groupState={data.status}
                    />
                  )}
                </div>
              </details>
            );
          })}
        </div>
        {data.invariant_errors?.map((error) => (
          <p role="alert" key={error} className="text-xs text-destructive">
            {error}
          </p>
        ))}
        <p className="text-xs leading-relaxed text-muted-foreground">
          {eligible.length > 0
            ? "This approves only the exact corrections and credit applications shown."
            : "Any correction requires its own exact proposal and human approval."}{" "}
          Each order retains its own approver, execution receipt and
          verification audit. Sales-order and cash settlement remain separate
          checks.
        </p>
      </div>
      {pending && eligible.length > 0 && (
        <div className="space-y-4 border-t bg-muted/20 p-5 sm:px-6">
          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={reviewed}
              disabled={disabled || blocked}
              onChange={(e) => setReviewed(e.target.checked)}
            />
            {hasRefundAllocation
              ? "I confirm the audited line changes belong to each displayed refund, and I reviewed every correction, its tax basis, financial effect and accounting conditions."
              : "I reviewed every correction, its financial effect and accounting conditions."}
          </label>
          <div className="flex flex-wrap justify-end gap-2">
            <button
              className="rounded-lg border px-4 py-2 text-xs font-medium disabled:opacity-50"
              disabled={disabled}
              onClick={onReject}
            >
              Reject group
            </button>
            <button
              className="rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-primary-foreground disabled:opacity-50"
              disabled={disabled || blocked || !reviewed || !eligible.length}
              onClick={onConfirm}
            >
              Approve {eligible.length} accounting corrections
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
