"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { useRejectResult } from "@/hooks/use-reconciliation";

/**
 * The one reject control, shared by every surface that can reach a result.
 *
 * Extracted from recon-results-table.tsx rather than copied. Two divergent reason
 * lists would split the label corpus into two incomparable sets, and the corpus is
 * the entire product of this feature — a second, drifting vocabulary is worse than
 * no second surface.
 *
 * Reachability is why this moved. page.tsx renders ReconResultsTable only inside
 * renderClassicBucketView(); for a tenant with recon_resolution_ui ON — Framework,
 * the only tenant with recon data — that lives behind the "Show all results (classic
 * view)" disclosure, so the primary review flow had no reject at all.
 */

// Operator language, not the wire values. The reviewer is deciding what they SAW;
// `wrong_match` is a database enum and reads like one.
//
// What is NOT shown here is deliberate: three of these five (wrong_match,
// wrong_amount, duplicate) feed the matcher's error rate and two do not.
// Surfacing that weighting would let a reviewer choose the reason that produces the
// number they want, and the corpus would stop being evidence about the matcher.
export const REJECT_REASONS: { value: string; label: string; hint: string }[] = [
  { value: "wrong_match", label: "Not the same money", hint: "Payout and deposit are unrelated" },
  { value: "wrong_amount", label: "Amounts don't reconcile", hint: "Right counterparty, wrong figures" },
  { value: "duplicate", label: "Already applied elsewhere", hint: "This deposit is a duplicate" },
  { value: "not_actionable", label: "Match is right — can't act on it", hint: "Something else blocks it" },
  { value: "other", label: "Something else", hint: "Needs a note" },
];

export const REASON_LABEL: Record<string, string> = Object.fromEntries(
  REJECT_REASONS.map((r) => [r.value, r.label])
);

// Mirrors backend TERMINAL_RESULT_STATUSES. A terminal row is immutable, so offering
// a control the API will refuse teaches the operator that the UI lies.
export const TERMINAL_STATUSES = new Set(["approved", "rejected", "locked", "carried_forward"]);

interface RejectMatchControlProps {
  /** ReconciliationResult id — NOT a proposal id. PATCH /results/{id}/reject keys on
   *  the result; the resolution surface holds both ids side by side on one object. */
  resultId: string;
  /** Closed run / recon disabled. Renders nothing rather than a control the API refuses. */
  disabled?: boolean;
  /** `icon` for the classic table's tight actions column; `inline` for the resolution
   *  worksheets, where it sits beside the text-and-icon "Investigate in chat". */
  variant?: "icon" | "inline";
}

export function RejectMatchControl({ resultId, disabled, variant = "icon" }: RejectMatchControlProps) {
  const rejectResult = useRejectResult();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  // Where to paint the picker. Captured from the button because the picker is
  // PORTALLED to <body> — see the render for why.
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  const close = () => {
    setOpen(false);
    setReason("");
    setNote("");
    setAnchor(null);
  };

  // Escape, outside-click, and scroll all close. Scroll matters because the picker is
  // position:fixed against coordinates captured at open time — once the page scrolls it
  // is pointing at nothing. Outside-click is also what keeps two rows' pickers from
  // being open at once now that each control owns its own state (the old row-keyed
  // version got that for free).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (dialogRef.current?.contains(t) || triggerRef.current?.contains(t)) return;
      close();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  if (disabled) return null;

  const toggle = (el: HTMLElement) => {
    if (open) return close();
    const r = el.getBoundingClientRect();
    setOpen(true);
    setAnchor({ top: r.bottom + 6, right: window.innerWidth - r.right });
  };

  // 'other' with a blank note is a 400 from the service. Enforcing it here means the
  // round trip that would fail never happens.
  const noteRequired = reason === "other";
  const canSubmit = reason !== "" && (!noteRequired || note.trim() !== "");

  const submit = () => {
    if (!canSubmit) return;
    rejectResult.mutate({
      result_id: resultId,
      reason,
      ...(note.trim() ? { note: note.trim() } : {}),
    });
    close();
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={(e) => toggle(e.currentTarget)}
        title="Reject match"
        aria-label="Reject match"
        aria-expanded={open}
        className={cn(
          "transition-colors",
          variant === "icon"
            ? cn("rounded p-1 text-red-600 hover:bg-red-50", open && "bg-red-50 ring-1 ring-red-600")
            : cn(
                "inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground hover:text-foreground",
                open && "border-red-600 text-red-600"
              )
        )}
      >
        <X className={variant === "icon" ? "h-4 w-4" : "h-3.5 w-3.5"} />
        {variant === "inline" && "Reject match"}
      </button>

      {/* PORTALLED to <body>, not rendered in the cell.
          Every surface that hosts this control sits inside an `overflow-x: auto`
          container, and that forces `overflow-y` to compute as `auto` rather than
          `visible` — so an absolutely-positioned picker got clipped after two of five
          reasons, with the note field and both buttons unreachable. jsdom has no layout
          engine and reported all eight tests green through it; the bug was only visible
          in a browser. A portal escapes every ancestor overflow, which is the only fix
          that does not constrain how the tables themselves are allowed to scroll. */}
      {open && anchor && typeof document !== "undefined" &&
        createPortal(
          <div
            ref={dialogRef}
            role="dialog"
            aria-label="Reject this match"
            style={{ position: "fixed", top: anchor.top, right: anchor.right }}
            className="z-50 w-72 rounded-lg border bg-card p-3 text-left shadow-lg"
          >
            <p className="text-xs font-semibold">Why is this wrong?</p>
            <p className="mt-0.5 mb-2 text-[11px] leading-snug text-muted-foreground">
              Pick the closest fit. This is the only signal we get about what the matcher
              gets wrong.
            </p>
            <div role="radiogroup" aria-label="Reason" className="flex flex-col gap-0.5">
              {REJECT_REASONS.map((r) => (
                <button
                  key={r.value}
                  type="button"
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
                type="button"
                onClick={close}
                className="rounded border px-3 py-1.5 text-xs text-muted-foreground hover:bg-muted"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={submit}
                disabled={!canSubmit}
                className="rounded bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:brightness-110 disabled:opacity-40"
              >
                Reject
              </button>
            </div>
          </div>,
          document.body
        )}
    </>
  );
}
