"use client";

import { useState } from "react";
import type { WriteConfirmationData } from "@/lib/types";
import { AccountingConfirmationCard } from "./accounting-confirmation-card";
import { creditAmount } from "./sales-credit-confirmation-card";

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
  const verified = eligible.filter(
    (m) =>
      m.card?.status === "approved" &&
      m.card.accounting_verification?.status === "verified",
  ).length;
  const pending = data.status === "pending";
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
              : data.status === "executing"
                ? "Running · awaiting results"
                : data.status === "rejected"
                  ? "Rejected"
                  : data.status === "indeterminate"
                    ? "Incomplete · review recorded outcomes"
                    : `${verified} / ${eligible.length} verified`}
          </span>
        </div>
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
          </div>
        )}
        <div className="space-y-3">
          {group.members.map((member) => {
            const card = member.card;
            const p = card?.accounting_review;
            const result =
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
                    {result}
                  </span>
                  {p?.kind === "sales_adjustment_credit" ? (
                    <span className="mt-1 block text-xs text-muted-foreground">
                      {p.profile.currency} · Sales Adjustments credit {creditAmount(p.expected_after.credit_total, p.profile.currency)} · Net invoice {creditAmount(p.expected_after.net_invoice_total, p.profile.currency)} · Tax impact {creditAmount(p.expected_after.credit_tax, p.profile.currency)}
                    </span>
                  ) : p?.kind === "invoice_sales_adjustment" ? (
                    <span className="mt-1 block text-xs text-muted-foreground">
                      {p.profile.currency} · Invoice discount {creditAmount(p.expected_after.discountTotal, p.profile.currency)} · Total {creditAmount(p.before.total, p.profile.currency)} → {creditAmount(p.expected_after.total, p.profile.currency)}
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
            I reviewed every correction, its financial effect and accounting
            conditions.
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
