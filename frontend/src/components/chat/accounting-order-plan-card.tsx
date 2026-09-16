"use client";

import { useId, useRef, type ReactNode } from "react";
import type { AccountingResolutionPlan, WriteConfirmationData } from "@/lib/types";

type Receipt = NonNullable<WriteConfirmationData["accounting_receipt"]>;

function money(value: unknown, currency?: string | null) {
  if ((typeof value !== "string" && typeof value !== "number") || String(value).trim() === "" || !Number.isFinite(Number(value))) return "—";
  return `${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}${currency ? ` ${currency}` : ""}`;
}

function checkedTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Time unavailable" : date.toLocaleString();
}

export function AccountingOrderPlanCard({ data, receipt = data.accounting_receipt, groupState, children }: {
  data: WriteConfirmationData;
  receipt?: Receipt;
  groupState?: WriteConfirmationData["status"];
  children: ReactNode;
}) {
  const detailId = useId();
  const details = useRef<HTMLDetailsElement>(null);
  const plan = receipt?.plan || data.accounting_review?.resolution_plan;
  if (!plan) return <>{children}</>;
  const original = data.accounting_receipt;
  const later = original && receipt && original.completion_audit_id !== receipt.completion_audit_id;
  const ready = data.status === "pending" && (!groupState || groupState === "pending");
  const reconciled = receipt?.status === "reconciled";
  const outcome = reconciled ? "Reconciled" : receipt ? "Further review required" : ready ? "Ready for review" : data.status === "executing" ? "Checking results" : data.status === "rejected" ? "Rejected" : data.status === "indeterminate" ? "Outcome unconfirmed" : data.status === "failed" ? "Needs review" : data.status === "pending" ? "Not submitted" : "Reconciliation pending";
  const amounts = receipt?.balance?.amounts;
  function stepStatus(step: AccountingResolutionPlan["steps"][number]) {
    if (step.status === "waiting" && step.depends_on.length && step.depends_on.every(id => plan!.steps.some(previous => previous.id === id && previous.status === "verified"))) return "evidence_required";
    if (receipt) return step.status;
    if (step.id !== plan!.active_step) return step.status;
    if (data.status === "approved") return data.accounting_verification?.status === "verified" ? "verified" : "needs_review";
    if (data.status === "executing" || data.status === "indeterminate") return "checking";
    if (data.status === "failed" || data.status === "rejected") return data.status;
    return ready ? "awaiting_approval" : "not_submitted";
  }
  const labels: Record<string, string> = { verified: "Verified", awaiting_approval: "Approval required", waiting: "Waiting for earlier steps", evidence_required: "Further evidence required", needs_review: "Needs review", checking: "Checking outcome", failed: "Needs review", rejected: "Rejected", not_submitted: "Not submitted" };
  return (
    <article className={`min-w-0 overflow-hidden rounded-2xl border bg-card ${reconciled ? "border-emerald-500/40" : "border-border"}`} aria-label={`Resolution plan ${plan.order_reference}`}>
      <header className="flex flex-wrap items-start justify-between gap-3 border-b p-5">
        <div><h4 className="text-lg font-semibold">Order {plan.order_reference}</h4><p className="mt-1 text-xs text-muted-foreground">{plan.currency || "Currency unverified"} · {plan.steps.length} resolution steps</p></div>
        <span role="status" className={`rounded-full border px-3 py-1 text-xs ${reconciled ? "text-emerald-500" : "text-muted-foreground"}`}>{outcome}</span>
      </header>
      <p className="bg-muted/20 px-5 py-4 text-sm leading-relaxed">{receipt?.summary || "Verify the posting correction, preserve sales-order consistency, then reconcile the complete order. Each new financial change requires its own exact approval."}</p>
      <div className="overflow-x-auto px-5">
        <table className="w-full text-left text-xs" aria-label={`Correction steps for ${plan.order_reference}`}>
          <thead className="text-muted-foreground"><tr><th className="py-3 pr-3 font-medium">Resolution step</th><th className="px-2 py-3 text-right font-medium">Before</th><th className="px-2 py-3 text-right font-medium">Target</th><th className="py-3 pl-3 font-medium">Status</th></tr></thead>
          <tbody>{plan.steps.map((step, index) => {
            const state = stepStatus(step);
            return <tr key={step.id} className="border-t align-top">
              <td className="py-4 pr-3"><strong className="font-medium">{index + 1}. {step.title}</strong><span className="mt-1 block max-w-xs leading-relaxed text-muted-foreground">{step.id === "reconcile" ? "Order total, VAT / tax and refunds" : step.affects_gl === true ? "Posting correction · affects the GL" : step.id === "posting" ? "Posting records · verification only" : "Source consistency · non-posting"}</span>{step.note && state !== "verified" && <span className="mt-1 block max-w-xs leading-relaxed text-muted-foreground">{step.note}</span>}</td>
              <td className="whitespace-nowrap px-2 py-4 text-right tabular-nums">{money(step.current_total, plan.currency)}</td>
              <td className="whitespace-nowrap px-2 py-4 text-right tabular-nums">{step.id === "reconcile" ? "Zero variance" : money(step.target_total, plan.currency)}</td>
              <td className={`py-4 pl-3 ${state === "verified" ? "text-emerald-500" : "text-muted-foreground"}`}>{labels[state] || "Needs review"}</td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      {receipt && <section className="space-y-3 border-t p-5 text-xs leading-relaxed" aria-label="Verified accounting result">
        <h5 className="text-sm font-semibold">{reconciled ? "Final reconciliation result" : "Verified progress and remaining work"}</h5>
        {amounts && <div className="overflow-x-auto"><table className="w-full tabular-nums" aria-label="Source and ERP comparison"><thead><tr className="text-muted-foreground"><th className="py-2 text-left font-medium">Comparison</th><th className="p-2 text-right font-medium">Source</th><th className="p-2 text-right font-medium">ERP</th><th className="py-2 pl-2 text-right font-medium">Variance</th></tr></thead><tbody>{[["order_total", "Order total"], ["tax", "VAT / tax"], ["refunds", "Refunds"]].map(([key, title]) => <tr className="border-t" key={key}><td className="py-2">{title}</td><td className="whitespace-nowrap p-2 text-right">{money(amounts[key]?.source)}</td><td className="whitespace-nowrap p-2 text-right">{money(amounts[key]?.target)}</td><td className="whitespace-nowrap py-2 pl-2 text-right">{money(amounts[key]?.delta)}</td></tr>)}</tbody></table><p className="mt-2 text-muted-foreground">Amounts in {plan.currency || "unverified currency"} · Variance = source − ERP</p></div>}
        <div className="flex flex-wrap gap-3">{receipt.record_links.map(link => <a key={`${link.record_type}:${link.record_id}`} className="text-primary underline" href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a>)}<a className="text-primary underline" href={receipt.reconciliation_url}>Reconciliation result</a><a className="text-primary underline" href={receipt.audit_url}>Audit log</a></div>
        {later && <div aria-label="Original correction approval"><p>Original correction approved by {original.approved_by.name} · {checkedTime(original.approved_at)}</p><p className="break-all text-muted-foreground">Original audit reference: {original.completion_audit_id}</p></div>}
        <p>{later ? "Latest correction approved by" : "Approved by"} {receipt.approved_by.name} · {checkedTime(receipt.approved_at)}</p>
        <p className="break-all text-muted-foreground">Audit reference: {receipt.completion_audit_id}</p>
        {receipt.next_step.status === "awaiting_approval" && <p>The next correction requires approval in its new card below.</p>}
        {receipt.next_step.reasons?.map(reason => <p key={reason}>{reason}</p>)}
        <p className="text-muted-foreground">Cash settlement remains a separate check.</p>
      </section>}
      <details ref={details} id={detailId} className="border-t p-5">
        <summary className="cursor-pointer text-xs font-medium">Exact correction, accounting basis and approval</summary>
        <div className="mt-4">{children}</div>
      </details>
      {ready && <footer className="flex flex-wrap items-center justify-between gap-3 border-t bg-muted/20 p-5"><p className="max-w-sm text-xs leading-relaxed text-muted-foreground">Review the exact change below. Independent orders can run in parallel; dependent corrections run in order.</p><button type="button" aria-controls={detailId} className="rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-primary-foreground" onClick={() => { if (details.current) { details.current.open = true; details.current.scrollIntoView({ behavior: "smooth", block: "start" }); } }}>Review exact correction →</button></footer>}
    </article>
  );
}
