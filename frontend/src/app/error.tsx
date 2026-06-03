"use client";

import { useEffect, useState } from "react";
import {
  isStaleChunkError,
  reloadOnceForStaleChunk,
} from "@/lib/recover-from-stale-chunk";

/**
 * Segment-level error boundary. Keeps the app shell; renders an inline
 * fallback for a single failed route segment.
 *
 * On a stale-chunk error (post-deploy bundle skew) it attempts a single guarded
 * reload. If a reload actually fires we render nothing briefly while the tab
 * navigates away. If the reload is BLOCKED (loop-guard, dev mode, or no
 * sessionStorage), we fall back to the same visible fallback as a non-chunk
 * error — never a dead-end blank page.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const stale = isStaleChunkError(error);
  // True only while an actual reload is navigating away — render nothing then.
  const [reloading, setReloading] = useState(false);

  useEffect(() => {
    if (stale && reloadOnceForStaleChunk()) setReloading(true);
  }, [stale]);

  if (reloading) return null;

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 p-8 text-center">
      <h2 className="text-xl font-semibold text-foreground">
        Something went wrong
      </h2>
      <p className="max-w-md text-[13px] text-muted-foreground">
        An unexpected error occurred. You can try again — if it keeps happening,
        reload the page.
      </p>
      <button
        type="button"
        onClick={() => (stale ? window.location.reload() : reset())}
        className="inline-flex h-9 items-center justify-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
      >
        Reload
      </button>
    </div>
  );
}
