"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { money as formatMoney } from "@/components/reconciliation/format";
import type { GroupBreakdownCause, GroupBreakdownData } from "@/lib/chat-stream";

/** The server's split of an issue group into causes. Every amount and count here is the
 * server's own; the model only explains the causes. A cause appears only when its rule held
 * for every order in it, and anything no rule explains is listed as "No shared cause". */

const STEP: Record<string, { pill: string; tone: string; bar: string }> = {
  none: { pill: "Nothing to do", tone: "bg-emerald-600/10 text-emerald-700 dark:text-emerald-400", bar: "bg-emerald-600" },
  reconciliation_rule: { pill: "Books right", tone: "bg-emerald-600/10 text-emerald-700 dark:text-emerald-400", bar: "bg-emerald-600" },
  recheck: { pill: "Recheck", tone: "bg-sky-600/10 text-sky-700 dark:text-sky-400", bar: "bg-sky-600" },
  settings_change: { pill: "Settings change", tone: "bg-amber-600/10 text-amber-700 dark:text-amber-400", bar: "bg-amber-600" },
  fix_at_source: { pill: "Fix at the source", tone: "bg-amber-600/10 text-amber-700 dark:text-amber-400", bar: "bg-amber-600" },
  prepare_corrections: { pill: "Group fix", tone: "bg-violet-600/10 text-violet-700 dark:text-violet-400", bar: "bg-violet-600" },
  needs_policy: { pill: "Needs a policy", tone: "bg-sky-600/10 text-sky-700 dark:text-sky-400", bar: "bg-sky-600" },
  review_individually: { pill: "Review", tone: "bg-muted text-muted-foreground", bar: "bg-muted-foreground/40" },
};
const FALLBACK = STEP.review_individually;

const NETSUITE: Record<string, string> = {
  complete: "invoices checked in NetSuite",
  not_needed: "no NetSuite reads needed",
  timed_out: "the NetSuite invoice check timed out, so that rule was skipped",
  unavailable: "NetSuite was unavailable, so the invoice rule was skipped",
};

/** The shared formatter, on the size of the difference; the direction is stated in words. */
function money(value: string | null | undefined, currency: string | null) {
  const amount = value === undefined || value === null ? NaN : Math.abs(Number(value));
  if (!Number.isFinite(amount)) return formatMoney(null);
  try {
    return formatMoney(String(amount), currency || "USD");
  } catch {
    return amount.toFixed(2);
  }
}

const METRIC_LABEL: Record<string, string> = { order_total: "order", tax: "tax", refunds: "refunds" };

function ActionLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link className="whitespace-nowrap text-[12.5px] font-medium text-primary underline" href={href}>
      {children}
    </Link>
  );
}

function compose(prompt: string) {
  return `/chat?${new URLSearchParams({ compose: prompt, new_session: "true" })}`;
}

function Action({ cause, data }: { cause: GroupBreakdownCause; data: GroupBreakdownData }) {
  // Each order with its exact case: the case tools need the case id, and a reference alone does not
  // identify a case across configurations.
  const refs = cause.order_references.map((ref, i) => (cause.case_ids?.[i] ? `${ref} (case ${cause.case_ids[i]})` : ref));
  if (cause.next_step === "review_individually") {
    return (
      <ActionLink
        href={compose(
          `Investigate these cases one at a time: ${refs.join(", ")}. Start with transaction_ops_status for each case_id and tell me what differs.`,
        )}
      >
        Review one by one →
      </ActionLink>
    );
  }
  if (cause.next_step === "prepare_corrections" && !(data.group_id && cause.orders === data.orders)) {
    // Part of a group, or one order: prepare exactly these orders. The group fix would prepare
    // every member of a mixed group, one by one, to find the few it can correct.
    return (
      <ActionLink
        href={compose(
          `Prepare corrections for these cases one at a time: ${refs.join(", ")}. For each, use transaction_ops_accounting_evidence with its case_id, then prepare the supported exact correction for my approval; show any order without a supported correction separately. Do not treat this request as financial approval.`,
        )}
      >
        Prepare these fixes →
      </ActionLink>
    );
  }
  if (cause.next_step === "prepare_corrections" && data.group_id) {
    const scope = Object.fromEntries(Object.entries(data.scope || {}).filter(([, value]) => value));
    return (
      <ActionLink
        href={compose(
          `Prepare fixes for issue group ${data.group_id}. Call transaction_ops.accounting_group with group_id "${data.group_id}"${
            Object.keys(scope).length ? ` and these exact scope parameters: ${JSON.stringify(scope)}` : ""
          }. Prepare supported exact corrections together for human approval; show every unsupported case separately. Do not treat this request or the group ID as financial approval.`,
        )}
      >
        Prepare these fixes →
      </ActionLink>
    );
  }
  if (cause.next_step === "settings_change") {
    return (
      <button
        type="button"
        disabled
        title="Settings changes are proposed from chat in a later release"
        className="rounded-md border px-2.5 py-1 text-[12.5px] font-medium opacity-60"
      >
        Propose settings change
      </button>
    );
  }
  return null;
}

export function GroupBreakdownCard({ data }: { data: GroupBreakdownData }) {
  const total = Math.max(data.orders, 1);
  const unknownTotal = data.totals.order_total === null || data.totals.tax === null;
  const difference = unknownTotal ? 0 : Number(data.totals.order_total ?? 0) || Number(data.totals.tax ?? 0);
  return (
    <section
      aria-label={`Breakdown of ${data.pattern || "the order"}`}
      className="overflow-hidden rounded-xl border border-amber-600/40 bg-card"
    >
      <div className="space-y-3 p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h3 className="text-[15px] font-semibold">{data.pattern || (data.case_id ? "This order" : "Issue group")}</h3>
            <p className="text-xs text-muted-foreground">
              <span className="tabular-nums">{data.orders}</span> {data.orders === 1 ? "order" : "orders"}
              {data.currency ? ` · ${data.currency}` : ""}
              {unknownTotal ? " · difference not fully known: some amounts are missing" : null}
              {difference ? (
                <>
                  {" "}
                  · {difference < 0 ? "NetSuite higher" : "Solidus higher"} by{" "}
                  <span className="tabular-nums">{money(String(difference), data.currency)}</span> in total
                </>
              ) : null}
            </p>
          </div>
          <span className="rounded-full bg-sky-600/10 px-2.5 py-0.5 text-xs font-semibold text-sky-700 dark:text-sky-400">
            {data.causes.length} {data.causes.length === 1 ? "cause" : "causes"} found
          </span>
        </div>
        <div
          className="flex h-2.5 gap-0.5 overflow-hidden rounded-full bg-muted"
          role="img"
          aria-label={data.causes.map((c) => `${c.orders} ${c.label}`).join(", ")}
        >
          {data.causes.map((cause) => (
            <span
              key={`${cause.cause}-${cause.label}-${cause.orders}`}
              className={`h-full ${(STEP[cause.next_step] || FALLBACK).bar}`}
              style={{ width: `${(cause.orders / total) * 100}%` }}
            />
          ))}
        </div>
      </div>
      <ul className="divide-y border-t">
        {data.causes.map((cause, index) => {
          const step = STEP[cause.next_step] || FALLBACK;
          const shown = cause.order_references.slice(0, 3);
          return (
            <li key={`${cause.cause}-${index}`} className="grid grid-cols-[10px_minmax(0,1fr)_auto] gap-x-3 gap-y-1 px-4 py-3">
              <span className={`mt-1.5 h-2.5 w-2.5 rounded-sm ${step.bar}`} aria-hidden />
              <div className="min-w-0">
                <h4 className="text-[14px] font-medium">{cause.label}</h4>
                <p className="mt-0.5 max-w-[64ch] text-[13px] text-muted-foreground">{cause.why}</p>
                {cause.facts.length > 0 && (
                  <p className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[12.5px] text-muted-foreground">
                    {cause.facts.map((fact) => (
                      <span key={fact.fact}>
                        <strong className="tabular-nums text-foreground">{fact.orders}</strong> {fact.fact}
                      </span>
                    ))}
                  </p>
                )}
              </div>
              <div className="text-right">
                <div className="text-[17px] font-semibold tabular-nums">{cause.orders}</div>
                <div className="whitespace-nowrap text-[12.5px] tabular-nums text-muted-foreground">
                  {money(cause.primary?.amount ?? cause.amounts.order_total, data.currency)}
                </div>
                {Object.keys(METRIC_LABEL)
                  .filter(
                    (metric) =>
                      metric !== (cause.primary?.metric ?? "order_total") &&
                      cause.amounts[metric] !== undefined &&
                      (cause.amounts[metric] === null || Number(cause.amounts[metric]) !== 0),
                  )
                  .map((metric) => (
                    <div key={metric} className="whitespace-nowrap text-[11.5px] tabular-nums text-muted-foreground">
                      {METRIC_LABEL[metric]}{" "}
                      {cause.amounts[metric] === null ? "unknown" : money(cause.amounts[metric], data.currency)}
                    </div>
                  ))}
                {cause.amounts.open_on_invoices !== undefined && (
                  <div className="whitespace-nowrap text-[11.5px] tabular-nums text-muted-foreground">
                    open {money(cause.amounts.open_on_invoices, data.currency)}
                  </div>
                )}
              </div>
              <div className="col-start-2 col-end-4 mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className={`rounded-full px-2 py-0.5 text-[11.5px] font-semibold ${step.tone}`}>
                  {cause.next_pill || step.pill}
                </span>
                <span className="text-[12.5px] text-muted-foreground">{cause.next_label}</span>
                <span className="font-mono text-[12px] text-muted-foreground">
                  {shown.join(" · ")}
                  {cause.order_references.length > shown.length ? ` +${cause.order_references.length - shown.length} more` : ""}
                </span>
                <Action cause={cause} data={data} />
              </div>
            </li>
          );
        })}
      </ul>
      <p className="border-t bg-muted/40 px-4 py-2 text-[12px] text-muted-foreground">
        Checked with saved evidence for <span className="tabular-nums">{data.checked.saved_evidence}</span> orders
        {data.checked.saved_source_orders ? ` (${data.checked.saved_source_orders} with the saved Solidus order)` : ""}
        {data.checked.saved_source === "unavailable" ? " · saved Solidus orders could not be read, so the Solidus rules were skipped" : ""}
        {` · ${NETSUITE[data.checked.netsuite] || data.checked.netsuite}`}
        {` · ${data.checked.seconds}s. A cause shows only when its rule held for every order in it.`}
      </p>
    </section>
  );
}
