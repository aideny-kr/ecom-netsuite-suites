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
 */

import { useRef } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Dialog, DialogPortal, DialogOverlay } from "@/components/ui/dialog";
import { useCeligoScript } from "@/hooks/use-celigo-flows";
import { queryState } from "@/lib/query-state";
import { CeligoScriptViewerBody } from "@/components/settings/celigo-script-viewer";
import { ErrorNotice } from "./shared";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

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
            "fixed inset-y-0 right-0 z-50 h-full w-[560px] max-w-[95vw] translate-x-0 translate-y-0",
            "overflow-y-auto rounded-none border-l bg-background p-4 shadow-lg duration-200",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right",
          )}
        >
          <DialogPrimitive.Title className="sr-only">Script source</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">
            Attachment sites and source for this script
          </DialogPrimitive.Description>

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
            <div className="pt-6">
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
