"use client";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { dateLabel, objectValue, safeError } from "../transaction-ops/format";
import { deltaValue } from "./variance";
import { useCaseResolution, type TransactionCase } from "./review-hooks";

/** Labels for case_service.RESOLUTION_REASONS (decided with Aiden, 2026-10-27 mock). */
export const RESOLUTION_REASONS: Record<string, string> = {
  fixed_in_netsuite_rest_in_source: "Fixed in NetSuite; the remaining difference is in Solidus",
  accepted_residual: "Accepted residual (below materiality)",
  handled_outside_app: "Handled outside the app (journal, write-off)",
  duplicate_or_test_order: "Duplicate or test order",
};
const textarea = "mt-1 block w-full rounded-md border bg-background p-2 text-[13px]";

/** The deltas of a remaining difference (case_service.remaining_difference: source, target and delta). */
function remaining(accepted: unknown) {
  const amounts = objectValue(objectValue(accepted).amounts);
  const delta = (key: string) => deltaValue(objectValue(amounts[key]).delta);
  return {
    total: delta("order_total").text,
    tax: delta("tax").text,
    refunds: delta("refunds"),
  };
}

function Amounts({ accepted }: { accepted: unknown }) {
  const r = remaining(accepted);
  return (
    <div className="grid grid-cols-3 gap-3 rounded-lg border p-4 text-[13px]">
      {(
        [
          ["Order total (Solidus − NetSuite)", r.total],
          ["Tax", r.tax],
          ["Refunds", r.refunds.text],
        ] as const
      ).map(([label, value]) => (
        <div key={label}>
          <span className="block text-xs text-muted-foreground">{label}</span>
          <span className="font-mono font-semibold tabular-nums">{value}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * A person who fixed an order outside the app re-checks it, then may accept what is left.
 * Only the person's click resolves a case; the server refuses a stale or changed difference.
 */
export function CaseResolution({
  c,
  recheck,
  checking,
  close,
}: {
  c: TransactionCase;
  recheck: () => void;
  checking: boolean;
  close: () => void;
}) {
  const { resolve, reopen } = useCaseResolution(c.id);
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  // The confirmation is for the exact amounts the person saw: a new offer clears it (Codex round 2).
  const [ackFor, setAckFor] = useState<string | null>(null);
  const offer = c.resolution_offer;
  const resolution = objectValue(c.resolution_json);

  if (c.status === "resolved") {
    const r = remaining(resolution.accepted);
    return (
      <section className="space-y-3">
        <div className="space-y-1 rounded-lg border p-4 text-[13px]">
          <p className="font-semibold">
            Resolved by {String(resolution.resolved_by_name || "a person")} ·{" "}
            {dateLabel(resolution.resolved_at)}
          </p>
          <p>Reason: {RESOLUTION_REASONS[String(resolution.reason)] || "Not provided"}.</p>
          <p>
            Accepted remaining: order total {r.total}, tax {r.tax}, refunds {r.refunds.text} · re-checked{" "}
            {dateLabel(resolution.observed_at)}
          </p>
          {typeof resolution.note === "string" && resolution.note && (
            <p className="text-muted-foreground">Note: {resolution.note}</p>
          )}
        </div>
        <p className="text-[13px] text-muted-foreground">
          It no longer appears in Needs review or the issue groups. If a later check finds a different
          remaining difference, it reopens.
        </p>
        <Button variant="outline" disabled={reopen.isPending} onClick={() => reopen.mutate()}>
          Reopen
        </Button>
        {reopen.error && <p role="alert">{safeError(reopen.error)}</p>}
      </section>
    );
  }
  if (c.status !== "open") return null;

  const reopened = objectValue(resolution.reopened);
  const notice = reopened.why ? (
    <p className="rounded-lg border p-4 text-[13px]">
      {reopened.why === "remaining_difference_changed"
        ? "Changed since it was resolved: a later check found a different remaining difference."
        : "Reopened by a person after it was resolved."}
    </p>
  ) : null;

  if (checking)
    return (
      <section className="space-y-2" role="status">
        {notice}
        <p className="font-semibold">Re-checking</p>
        <p className="text-[13px] text-muted-foreground">
          Reading NetSuite and Solidus again for this one order. The result appears here when the
          re-check finishes.
        </p>
      </section>
    );

  if (offer?.available) {
    const r = remaining(offer.accepted);
    const shown = JSON.stringify(offer.accepted);
    const ack = ackFor === shown;
    const statement = `I accept the remaining difference of ${r.total} (order total) and ${r.tax} (tax)${
      r.refunds.nonzero ? ` and ${r.refunds.text} (refunds)` : ""
    } as shown.`;
    return (
      <section className="space-y-4">
        {notice}
        <p className="font-semibold">Re-checked {dateLabel(c.last_observed_at)} · Still differs</p>
        <Amounts accepted={offer.accepted} />
        <p className="text-xs text-muted-foreground">
          The detail is in the comparison below. Accepting writes nothing to NetSuite or Solidus. The
          offer expires at {dateLabel(offer.fresh_until)}; after that, re-check again.
        </p>
        <label className="block text-[13px]">
          Reason
          <select
            className="mt-1 block h-10 w-full rounded-md border bg-background px-3 text-[13px]"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          >
            <option value="">Choose a reason</option>
            {offer.reasons.map((code) => (
              <option key={code} value={code}>
                {RESOLUTION_REASONS[code] || code}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-[13px]">
          Note <span className="text-xs text-muted-foreground">Optional, for people only</span>
          <textarea
            className={textarea}
            maxLength={1000}
            rows={3}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        </label>
        <label className="flex items-start gap-2 text-[13px]">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={ack}
            onChange={(e) => setAckFor(e.target.checked ? shown : null)}
          />
          <span>{statement}</span>
        </label>
        <div className="flex flex-wrap gap-3">
          <Button
            disabled={!reason || !ack || resolve.isPending}
            onClick={() =>
              resolve.mutate({ reason, note, accepted: JSON.parse(shown) as typeof offer.accepted })
            }
          >
            Accept as resolved
          </Button>
          <Button variant="outline" onClick={close}>
            Keep in Needs review
          </Button>
        </div>
        {resolve.error && <p role="alert">{safeError(resolve.error)}</p>}
      </section>
    );
  }

  if (offer?.unavailable_reason === "difference_not_measured")
    return (
      <section className="space-y-3">
        {notice}
        <p className="rounded-lg border p-4 text-[13px] text-muted-foreground">
          This order&apos;s evidence is incomplete, so what is left cannot be accepted as resolved.
          Re-check it, or work through it with the agent.
        </p>
      </section>
    );
  if (offer?.unavailable_reason !== "recheck_required") return notice;
  return (
    <section className="space-y-3">
      {notice}
      <div className="space-y-1 rounded-lg border p-4 text-[13px]">
        <p className="font-semibold">Fixed this outside the app?</p>
        <p className="text-muted-foreground">
          Re-check reads this order again from NetSuite and Solidus. If it matches, it is reconciled.
          If not, you see exactly what is left and can accept it as resolved.
        </p>
      </div>
      <label className="block text-[13px]">
        What did you change?{" "}
        <span className="text-xs text-muted-foreground">Optional, kept with the resolution</span>
        <textarea
          className={textarea}
          maxLength={1000}
          rows={2}
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <Button onClick={recheck}>I fixed this — re-check</Button>
    </section>
  );
}
