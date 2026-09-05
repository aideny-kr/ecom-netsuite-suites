"use client";

/**
 * Task 17 — the script drawer (mockup screen 4): the existing script
 * viewer, re-homed from a centered `Dialog` into a right-hand panel that
 * slides in OVER the inspector rather than jumping the whole page. Reached
 * via `&script=<scriptId>` (`celigo-route.ts`'s `go.script`), mounted by
 * `celigo-flow-page.tsx` whenever `route.scriptId` is set.
 *
 * Renders `CeligoScriptViewerBody` (`components/settings/celigo-script-
 * viewer.tsx`) unchanged — same inert `react-syntax-highlighter` rendering,
 * same N2 shield banner, same used-by table. This file owns only the
 * drawer CHROME (position, size, focus management) and this query's own
 * pending/error gating; see that file's docstring for the security
 * contract on `content` (untrusted, inert-only, never templated/eval'd/
 * logged).
 *
 * Escape: no keydown listener here at all. Radix's `Dialog` already closes
 * on Escape via its own `DismissableLayer` (a document-level listener,
 * `onOpenChange(false)` by default) — this is the SAME reasoning
 * `celigo-command-palette.tsx`'s docstring already gives for its own
 * Escape handling. `celigo-flow-page.tsx` (Task 14) separately owns the
 * "drawer closes first, a second Escape then clears the step" ORDERING —
 * its own `window` keydown listener checks `route.scriptId` before
 * `route.stepId` and calls the exact same `onClose` (`go.script(null)`)
 * this drawer's own dismissal calls. Both simply agree on what "closed"
 * means; this file does not re-implement that ordering, it only needs to
 * close itself.
 *
 * ═══ Sizing UI (approved mock: docs/superpowers/mockups/
 * 2026-09-04-celigo-flow-sizing.html) ═══
 * The drawer used to be a fixed `w-[560px] max-w-[95vw]`. It now carries
 * its own remembered width, dragged from a grip on its LEFT edge, plus a
 * session-only "maximize" mode layered on top:
 *
 * - `width` (px) is read ONCE from `localStorage["celigo.scriptDrawerWidth"]`
 *   at mount (default 560, clamped to `[MIN_DRAWER_WIDTH, 96% of the
 *   viewport]`), re-clamped on window resize, and applied as an inline
 *   style — never a Tailwind class, since the value is per-viewer data, not
 *   a fixed design token. Every localStorage read/write is try/catched: a
 *   viewer with storage disabled still gets a working, just
 *   non-persistent, resize.
 * - The grip drags via Pointer Events (`setPointerCapture`, so the drag
 *   keeps tracking even if the pointer leaves the 11px hit target) and is
 *   ALSO a `role="separator"` taking keyboard focus: ArrowLeft widens /
 *   ArrowRight narrows by `WIDTH_STEP` (24px), Home/End jump to the
 *   min/max. Persisted on drag END (pointerup) but immediately on a
 *   keyboard move — a keyboard press has no separate "end" event to persist
 *   from.
 * - `maximized` is a plain `useState`, deliberately never touching
 *   localStorage: it is a MODE on top of the remembered width, not a
 *   width of its own. Leaving maximize re-applies whatever `width` already
 *   was (dragged, arrowed, or the stored default) — the two facts are
 *   independent, exactly like `celigo-flow-page.tsx`'s `flowHeaderCollapsed`
 *   / `flowHeaderSize` pair.
 * - The code area's font can't be grown from here directly —
 *   `CeligoScriptViewerBody` (a different file, not owned by this task)
 *   sets `react-syntax-highlighter`'s font size via its own inline
 *   `customStyle`, which only a stylesheet `!important` rule can outrank.
 *   The scoped `<style>` block below does exactly that, gated on the
 *   `celigo-script-drawer--maximized` class this file toggles — no other
 *   file needs to change for "maximized code reads one step larger" to
 *   hold.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Dialog, DialogPortal, DialogOverlay } from "@/components/ui/dialog";
import { useCeligoScript } from "@/hooks/use-celigo-flows";
import { queryState } from "@/lib/query-state";
import { CeligoScriptViewerBody } from "@/components/settings/celigo-script-viewer";
import { ErrorNotice } from "./shared";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

const DRAWER_WIDTH_STORAGE_KEY = "celigo.scriptDrawerWidth";
const MIN_DRAWER_WIDTH = 480;
const DEFAULT_DRAWER_WIDTH = 560;
const DRAWER_WIDTH_STEP = 24;

/** 96% of the current viewport — the drawer's own upper bound, re-derived on
 * every clamp rather than cached, so a window resize (or a narrower jsdom
 * default) is always honoured without a separate "recompute the max" path. */
function maxDrawerWidth(): number {
  if (typeof window === "undefined") return Infinity;
  return window.innerWidth * 0.96;
}

function clampDrawerWidth(width: number): number {
  // Clamp to the max FIRST: on a viewport narrower than
  // `MIN_DRAWER_WIDTH / 0.96` the two bounds invert, and the minimum must
  // still win — a drawer narrower than 480px is unusable, a drawer wider
  // than 96% of a tiny viewport is merely cramped.
  return Math.max(MIN_DRAWER_WIDTH, Math.min(width, maxDrawerWidth()));
}

function readStoredDrawerWidth(): number {
  if (typeof window === "undefined") return DEFAULT_DRAWER_WIDTH;
  try {
    const raw = window.localStorage.getItem(DRAWER_WIDTH_STORAGE_KEY);
    const parsed = raw !== null ? parseInt(raw, 10) : NaN;
    if (!Number.isFinite(parsed)) return DEFAULT_DRAWER_WIDTH;
    return clampDrawerWidth(parsed);
  } catch {
    return DEFAULT_DRAWER_WIDTH;
  }
}

function persistDrawerWidth(width: number): void {
  try {
    window.localStorage.setItem(DRAWER_WIDTH_STORAGE_KEY, String(Math.round(width)));
  } catch {
    // Best effort — the width still applies for this render, it just won't
    // survive a reload.
  }
}

function DrawerSkeleton(): JSX.Element {
  return (
    <div aria-hidden="true" className="flex flex-col gap-3 pt-8">
      <div className="h-5 w-40 animate-pulse rounded bg-muted" />
      <div className="h-4 w-64 animate-pulse rounded bg-muted" />
      <div className="h-48 animate-pulse rounded-lg border bg-card" />
    </div>
  );
}

export function CeligoScriptDrawer({
  scriptId,
  onClose,
  returnFocusTo,
  currentStepId,
  currentJsonPath,
}: {
  scriptId: string | null;
  onClose: () => void;
  returnFocusTo?: React.RefObject<HTMLElement>;
  /** The step this drawer was opened FROM. One script can be attached at
   * several sites; without this the body announces `used_by[0]`'s hook
   * whichever step the reader actually came from. */
  currentStepId?: string | null;
  /** The SITE within that step — the clicked attachment's `json_path`. A step
   * can hold two sites of the same script (preMap and postMap, or two clones
   * of one family), which `currentStepId` alone cannot separate. */
  currentJsonPath?: string | null;
}): JSX.Element {
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  const dragStateRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null);
  const [width, setWidth] = useState<number>(readStoredDrawerWidth);
  const [maximized, setMaximized] = useState(false);
  const [dragging, setDragging] = useState(false);

  // A width that fit a wider viewport must not leave the drawer wider than
  // 96% of a viewport that just got narrower (window resized, or rotated on
  // a tablet) — re-clamp on every resize the same way the initial read does.
  useEffect(() => {
    function onWindowResize() {
      setWidth((w) => clampDrawerWidth(w));
    }
    window.addEventListener("resize", onWindowResize);
    return () => window.removeEventListener("resize", onWindowResize);
  }, []);

  const handleGripPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      dragStateRef.current = { pointerId: e.pointerId, startX: e.clientX, startWidth: width };
      setDragging(true);
      // `?.()` is a safe no-op in jsdom, which implements neither method.
      e.currentTarget.setPointerCapture?.(e.pointerId);
    },
    [width],
  );

  const handleGripPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragStateRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    // The grip sits on the drawer's LEFT edge, so dragging it further LEFT
    // (a SMALLER clientX) widens the drawer — the mock's own
    // `setW(startW - (e.clientX - startX))`.
    setWidth(clampDrawerWidth(drag.startWidth - (e.clientX - drag.startX)));
  }, []);

  const handleGripPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragStateRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    dragStateRef.current = null;
    setDragging(false);
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    // Persisted on drag END, not per-move — a rapid drag would otherwise
    // hammer localStorage on every pointermove.
    setWidth((w) => {
      persistDrawerWidth(w);
      return w;
    });
  }, []);

  const handleGripKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      let next: number | null = null;
      if (e.key === "ArrowLeft") next = width + DRAWER_WIDTH_STEP;
      else if (e.key === "ArrowRight") next = width - DRAWER_WIDTH_STEP;
      else if (e.key === "Home") next = MIN_DRAWER_WIDTH;
      else if (e.key === "End") next = maxDrawerWidth();
      if (next === null) return;
      e.preventDefault();
      const clamped = clampDrawerWidth(next);
      setWidth(clamped);
      // A keyboard move has no separate "end" event to persist from — apply
      // and persist together, unlike the drag handlers above.
      persistDrawerWidth(clamped);
    },
    [width],
  );

  const toggleMaximize = useCallback(() => {
    // Deliberately never touches localStorage — maximize is a MODE on top
    // of the remembered width, not a width of its own (see the file
    // docstring). `width` itself is untouched by this toggle, so leaving
    // maximize simply re-applies whatever it already was.
    setMaximized((prev) => !prev);
  }, []);

  // `useCeligoScript(undefined)` is `enabled: false` (see
  // `use-celigo-flows.ts`), so this is a harmless no-op fetch while closed.
  const scriptQuery = useCeligoScript(scriptId ?? undefined);
  const scriptState = queryState(scriptQuery);
  const script = scriptQuery.data;

  // `open={!!scriptId}` (never an early `return null`) so Radix's own
  // Presence controls mount/unmount -- the same pattern
  // `CeligoScriptViewerDialog` already uses. That's what makes
  // `onCloseAutoFocus` (below) actually fire on close: Radix runs its own
  // unmount/focus-restore lifecycle on an `open` TRANSITION, which an
  // abrupt React-level unmount would skip entirely.
  return (
    <Dialog
      open={!!scriptId}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogPortal>
        <DialogOverlay />
        <DialogPrimitive.Content
          aria-label="Script source"
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            closeButtonRef.current?.focus();
          }}
          onCloseAutoFocus={(e) => {
            if (returnFocusTo?.current) {
              e.preventDefault();
              returnFocusTo.current.focus();
            }
          }}
          className={cn(
            "fixed inset-y-0 right-0 z-50 h-full translate-x-0 translate-y-0",
            "overflow-y-auto rounded-none border-l bg-background p-4 shadow-lg duration-200",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right",
            // Maximized takes the whole width via a class (matching the
            // mock's `.drawer.maximized { width: 100% !important }`);
            // otherwise the dynamic per-viewer width below is an inline
            // style, never a fixed Tailwind token.
            maximized ? "w-full max-w-full" : "max-w-full",
          )}
          style={maximized ? undefined : { width: `${width}px` }}
        >
          {/* Outranks `CeligoScriptViewerBody`'s own inline
              `customStyle={{ fontSize: "12px" }}` on the syntax highlighter
              — a stylesheet `!important` rule is the only thing that can,
              since that file is not owned by this task (see the file
              docstring). Scoped to this drawer's own maximized class so it
              never leaks to any other `<pre>` on the page. */}
          <style>{`.celigo-script-drawer--maximized pre { font-size: 12.5px !important; }`}</style>

          {!maximized && (
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize drawer"
              aria-valuemin={MIN_DRAWER_WIDTH}
              aria-valuemax={Math.round(maxDrawerWidth())}
              aria-valuenow={Math.round(width)}
              tabIndex={0}
              onPointerDown={handleGripPointerDown}
              onPointerMove={handleGripPointerMove}
              onPointerUp={handleGripPointerUp}
              onKeyDown={handleGripKeyDown}
              className={cn(
                "absolute -left-[5px] top-0 bottom-0 z-10 w-[11px] cursor-col-resize touch-none",
                "focus:outline-none",
              )}
            >
              <span
                aria-hidden="true"
                className={cn(
                  "absolute left-1/2 top-1/2 h-11 w-[3px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-border",
                  dragging && "bg-accent",
                )}
              />
            </div>
          )}

          <DialogPrimitive.Title className="sr-only">Script source</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">
            Attachment sites and source for this script
          </DialogPrimitive.Description>

          <button
            type="button"
            aria-pressed={maximized}
            aria-label={maximized ? "Restore width" : "Maximize"}
            title={maximized ? "Restore width" : "Maximize"}
            onClick={toggleMaximize}
            className={cn(
              "absolute right-10 top-3 flex h-6 w-6 items-center justify-center rounded-sm text-[13px] opacity-70",
              "ring-offset-background transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
              maximized && "text-accent-foreground opacity-100",
            )}
          >
            <span aria-hidden="true">{maximized ? "⤡" : "⤢"}</span>
          </button>

          <DialogPrimitive.Close
            ref={closeButtonRef}
            className="absolute right-3 top-3 rounded-sm opacity-70 ring-offset-background transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
          >
            <X className="h-4 w-4" />
            <span className="sr-only">Close</span>
          </DialogPrimitive.Close>

          {scriptState === "error" ? (
            <div className="flex flex-col items-center gap-3 pt-8">
              <ErrorNotice message="Couldn't load this script." onRetry={() => scriptQuery.refetch()} />
            </div>
          ) : scriptState !== "success" || !script ? (
            <DrawerSkeleton />
          ) : (
            <div className={cn("pt-6", maximized && "celigo-script-drawer--maximized")}>
              {/* The selected step, passed straight through: the body picks
                  out that step's own `used_by` site so the header names the
                  hook the reader actually opened, and still falls back to
                  `used_by[0]` when nothing is selected. */}
              <CeligoScriptViewerBody
                script={script}
                currentStepId={currentStepId}
                currentJsonPath={currentJsonPath}
              />
            </div>
          )}
        </DialogPrimitive.Content>
      </DialogPortal>
    </Dialog>
  );
}
