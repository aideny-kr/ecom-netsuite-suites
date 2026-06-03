"use client";

import { useEffect, useState } from "react";
import {
  isStaleChunkError,
  reloadOnceForStaleChunk,
} from "@/lib/recover-from-stale-chunk";

/**
 * Root error boundary. Catches errors thrown in the root layout / uncaught at
 * the top of the tree. Must render its own <html>/<body> because it replaces
 * the entire document.
 *
 * On a stale-chunk error (post-deploy bundle skew) it attempts a single guarded
 * reload. If a reload actually fires we render a blank body briefly while the
 * tab navigates away. If the reload is BLOCKED (loop-guard, dev mode, or no
 * sessionStorage), we show the SAME branded full-page fallback as a non-chunk
 * error — never a dead-end empty <body>. The Reload button hard-reloads (the
 * root shell is gone, so a fresh load is the most reliable recovery).
 */
export default function GlobalError({
  error,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const stale = isStaleChunkError(error);
  // True only while an actual reload is navigating away — render a blank body.
  const [reloading, setReloading] = useState(false);

  useEffect(() => {
    if (stale && reloadOnceForStaleChunk()) setReloading(true);
  }, [stale]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: "1rem",
          padding: "2rem",
          textAlign: "center",
          fontFamily:
            "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif",
          background: "#0a0a0a",
          color: "#fafafa",
        }}
      >
        {!reloading && (
          <>
            <h2 style={{ fontSize: "1.25rem", fontWeight: 600, margin: 0 }}>
              Something went wrong
            </h2>
            <p
              style={{
                maxWidth: "28rem",
                fontSize: "13px",
                color: "#a1a1aa",
                margin: 0,
              }}
            >
              An unexpected error occurred. Reload the page to continue.
            </p>
            <button
              type="button"
              onClick={() => window.location.reload()}
              style={{
                height: "2.25rem",
                padding: "0 1rem",
                borderRadius: "0.375rem",
                border: "none",
                background: "#2563eb",
                color: "#fff",
                fontSize: "0.875rem",
                fontWeight: 500,
                cursor: "pointer",
              }}
            >
              Reload
            </button>
          </>
        )}
      </body>
    </html>
  );
}
