"use client";

import { useState } from "react";
import { createPortal } from "react-dom";
import { MessageSquare, Check, X } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ReconResult } from "@/lib/types";
import { useApproveResult, useRejectResult } from "@/hooks/use-reconciliation";

interface ReconResultsTableProps {
  results: ReconResult[];
  onInvestigate?: (result: ReconResult) => void;
}

// Operator language, not the wire values. The reviewer is deciding what they SAW;
// `wrong_match` is a database enum and reads like one.
//
// What is NOT shown here is deliberate: three of these five (wrong_match,
// wrong_amount, duplicate) feed the matcher's false-positive rate and two do not.
// Surfacing that weighting would let a reviewer choose the reason that produces the
// number they want, and the corpus would stop being evidence about the matcher.
const REJECT_REASONS: { value: string; label: string; hint: string }[] = [
  { value: "wrong_match", label: "Not the same money", hint: "Payout and deposit are unrelated" },
  { value: "wrong_amount", label: "Amounts don't reconcile", hint: "Right counterparty, wrong figures" },
  { value: "duplicate", label: "Already applied elsewhere", hint: "This deposit is a duplicate" },
  { value: "not_actionable", label: "Match is right — can't act on it", hint: "Something else blocks it" },
  { value: "other", label: "Something else", hint: "Needs a note" },
];

const REASON_LABEL: Record<string, string> = Object.fromEntries(
  REJECT_REASONS.map((r) => [r.value, r.label])
);

// Mirrors backend TERMINAL_RESULT_STATUSES. A terminal row is immutable, so offering
// a control the API will refuse teaches the operator that the UI lies.
const TERMINAL_STATUSES = new Set(["approved", "rejected", "locked", "carried_forward"]);

const statusColors: Record<string, string> = {
  auto_matched: "bg-green-100 text-green-800",
  suggested: "bg-orange-100 text-orange-800",
  pending: "bg-red-100 text-red-800",
  approved: "bg-blue-100 text-blue-800",
  locked: "bg-gray-100 text-gray-800",
  // Missing until 2026-08-06: 'rejected' fell through to the bare `bg-gray-100`
  // fallback, which sets no text colour — the badge rendered as an unreadable
  // white-on-white blob. Invisible to jsdom, obvious in a browser.
  rejected: "bg-red-100 text-red-800",
  carried_forward: "bg-amber-100 text-amber-800",
};

export function ReconResultsTable({ results, onInvestigate }: ReconResultsTableProps) {
  const approveResult = useApproveResult();
  const rejectResult = useRejectResult();
  // Only one picker open at a time, keyed by row id.
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  const [reason, setReason] = useState<string>("");
  const [note, setNote] = useState<string>("");
  // Where to paint the picker. Captured from the button because the picker is
  // PORTALLED to <body> — see the render for why.
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);

  const closePicker = () => {
    setRejectingId(null);
    setReason("");
    setNote("");
    setAnchor(null);
  };

  const openPicker = (id: string, el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    closePicker();
    setRejectingId(id);
    setAnchor({ top: r.bottom + 6, right: window.innerWidth - r.right });
  };

  // 'other' with a blank note is a 400 from the service. Enforcing it here means the
  // round trip that would fail never happens.
  const noteRequired = reason === "other";
  const canSubmit = reason !== "" && (!noteRequired || note.trim() !== "");

  const submitReject = (resultId: string) => {
    if (!canSubmit) return;
    rejectResult.mutate({
      result_id: resultId,
      reason,
      ...(note.trim() ? { note: note.trim() } : {}),
    });
    closePicker();
  };

  if (results.length === 0) {
    return (
      <div className="rounded-xl border bg-card p-8 text-center text-muted-foreground shadow-soft">
        No results to display.
      </div>
    );
  }

  return (
    // NOT overflow-hidden. The reject picker is absolutely positioned inside a cell,
    // and clipping the card cut it off after two of five reasons — no note field, no
    // buttons. A jsdom test cannot see this: it has no layout engine, so it happily
    // "clicks" an element that a real browser never paints.
    // overflow-x-auto keeps the wide table scrollable without clipping vertically.
    <div className="rounded-xl border bg-card shadow-soft overflow-x-auto">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b bg-muted/50">
            <th className="px-4 py-3 text-left font-medium text-muted-foreground">Status</th>
            <th className="px-4 py-3 text-left font-medium text-muted-foreground">Order</th>
            <th className="px-4 py-3 text-left font-medium text-muted-foreground">Match</th>
            <th className="px-4 py-3 text-right font-medium text-muted-foreground">Stripe</th>
            <th className="px-4 py-3 text-right font-medium text-muted-foreground">NetSuite</th>
            <th className="px-4 py-3 text-right font-medium text-muted-foreground">Variance</th>
            {/* R2: the persisted value is the advisory composite — "Confidence" reads
                as an engine verdict; the Status badge is the authoritative disposition. */}
            <th className="px-4 py-3 text-center font-medium text-muted-foreground">Advisory Score</th>
            <th className="px-4 py-3 text-center font-medium text-muted-foreground">Actions</th>
          </tr>
        </thead>
        <tbody>
          {results.map((result) => (
            <tr key={result.id} className="border-b last:border-0 hover:bg-muted/30 transition-colors">
              <td className="px-4 py-3">
                <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", statusColors[result.status] || "bg-gray-100")}>
                  {result.status}
                </span>
                {/* A terminal row with no visible reason sends the reviewer to the
                    audit log to answer "why is this rejected?". Keep it on the row. */}
                {result.status === "rejected" && result.reject_reason && (
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    {REASON_LABEL[result.reject_reason] ?? result.reject_reason}
                  </div>
                )}
              </td>
              <td className="px-4 py-3">
                {result.evidence?.order_reference ? (
                  <span className="font-mono text-[11px] text-foreground">{result.evidence.order_reference}</span>
                ) : result.evidence?.charge_source_id ? (
                  <span className="font-mono text-[11px] text-muted-foreground">{result.evidence.charge_source_id.slice(0, 14)}...</span>
                ) : (
                  <span className="text-muted-foreground">-</span>
                )}
              </td>
              <td className="px-4 py-3 text-foreground">{result.match_type}</td>
              <td className="px-4 py-3 text-right font-mono text-foreground">
                {result.stripe_amount != null ? `$${Number(result.stripe_amount).toFixed(2)}` : "-"}
              </td>
              <td className="px-4 py-3 text-right font-mono text-foreground">
                {result.netsuite_amount != null ? `$${Number(result.netsuite_amount).toFixed(2)}` : "-"}
              </td>
              <td className="px-4 py-3 text-right font-mono text-foreground">
                {Number(result.variance_amount) > 0 ? (
                  <span className="text-red-600">${Number(result.variance_amount).toFixed(2)}</span>
                ) : (
                  "$0.00"
                )}
              </td>
              <td className="px-4 py-3 text-center">
                <span
                  className="font-mono text-xs text-muted-foreground"
                  title="Advisory match score (amount + timing agreement). The Status badge reflects the authoritative disposition."
                >
                  {(Number(result.confidence) * 100).toFixed(0)}%
                </span>
              </td>
              <td className="relative px-4 py-3 text-center">
                <div className="flex items-center justify-center gap-1">
                  {result.status === "suggested" && (
                    <button
                      onClick={() => approveResult.mutate({ result_id: result.id })}
                      className="rounded p-1 text-green-600 hover:bg-green-50 transition-colors"
                      title="Approve match"
                    >
                      <Check className="h-4 w-4" />
                    </button>
                  )}
                  {!TERMINAL_STATUSES.has(result.status) && (
                    <button
                      onClick={(e) =>
                        rejectingId === result.id
                          ? closePicker()
                          : openPicker(result.id, e.currentTarget)
                      }
                      className={cn(
                        "rounded p-1 text-red-600 hover:bg-red-50 transition-colors",
                        rejectingId === result.id && "bg-red-50 ring-1 ring-red-600"
                      )}
                      title="Reject match"
                      aria-label="Reject match"
                      aria-expanded={rejectingId === result.id}
                    >
                      <X className="h-4 w-4" />
                    </button>
                  )}
                  {(result.status === "pending" || result.status === "suggested") && onInvestigate && (
                    <button
                      onClick={() => onInvestigate(result)}
                      className="rounded p-1 text-blue-600 hover:bg-blue-50 transition-colors"
                      title="Investigate in Chat"
                    >
                      <MessageSquare className="h-4 w-4" />
                    </button>
                  )}
                </div>

                {/* PORTALLED to <body>, not rendered in the cell.
                    The card scrolls horizontally, and `overflow-x: auto` forces
                    `overflow-y` to compute as `auto` rather than `visible` — so an
                    absolutely-positioned picker got clipped after two of five reasons,
                    with the note field and both buttons unreachable. jsdom has no
                    layout engine and reported all eight tests green through it; the
                    bug was only visible in a browser. A portal escapes every ancestor
                    overflow, which is the only fix that does not constrain how the
                    table itself is allowed to scroll. */}
                {rejectingId === result.id && anchor && typeof document !== "undefined" &&
                  createPortal(
                  <div
                    role="dialog"
                    aria-label="Reject this match"
                    style={{ position: "fixed", top: anchor.top, right: anchor.right }}
                    className="z-50 w-72 rounded-lg border bg-card p-3 text-left shadow-lg"
                  >
                    <p className="text-xs font-semibold">Why is this wrong?</p>
                    <p className="mt-0.5 mb-2 text-[11px] leading-snug text-muted-foreground">
                      Pick the closest fit. This is the only signal we get about what the
                      matcher gets wrong.
                    </p>
                    <div role="radiogroup" aria-label="Reason" className="flex flex-col gap-0.5">
                      {REJECT_REASONS.map((r) => (
                        <button
                          key={r.value}
                          role="radio"
                          aria-checked={reason === r.value}
                          aria-label={r.label}
                          onClick={() => setReason(r.value)}
                          className={cn(
                            "flex items-baseline gap-2 rounded border border-transparent px-2 py-1.5 text-left text-xs hover:bg-muted",
                            reason === r.value && "border-red-600 bg-red-50"
                          )}
                        >
                          <span
                            className={cn(
                              "mt-0.5 h-2.5 w-2.5 flex-none rounded-full border",
                              reason === r.value ? "border-red-600 bg-red-600" : "border-muted-foreground"
                            )}
                          />
                          <span className="flex flex-col">
                            <span className="font-medium">{r.label}</span>
                            <span className="text-[11px] leading-tight text-muted-foreground">{r.hint}</span>
                          </span>
                        </button>
                      ))}
                    </div>
                    <label className="mt-2 block text-[11px] text-muted-foreground">
                      Note{" "}
                      {noteRequired ? (
                        <span className="font-semibold text-red-600">(required)</span>
                      ) : (
                        <span>(optional)</span>
                      )}
                    </label>
                    <textarea
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                      placeholder="What did you see?"
                      className="mt-1 min-h-[48px] w-full resize-y rounded border bg-background p-2 text-xs"
                    />
                    <div className="mt-2 flex justify-end gap-2">
                      <button
                        onClick={closePicker}
                        className="rounded border px-3 py-1.5 text-xs text-muted-foreground hover:bg-muted"
                      >
                        Cancel
                      </button>
                      <button
                        onClick={() => submitReject(result.id)}
                        disabled={!canSubmit}
                        className="rounded bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:brightness-110 disabled:opacity-40"
                      >
                        Reject
                      </button>
                    </div>
                  </div>,
                  document.body
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
