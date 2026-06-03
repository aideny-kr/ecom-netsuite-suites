"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { BUILD_ID } from "@/lib/build-id";

const CHECK_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes

/**
 * Detects a deployed-vs-running frontend version skew.
 *
 * Polls the same-origin `/version` route (the deployed container's build id)
 * and compares it against this bundle's inlined {@link BUILD_ID}. When they
 * differ, a new version is live and the open tab is stale.
 *
 * - `/version` is a same-origin Next route, so we `fetch` it directly with
 *   `cache: "no-store"` (NOT the backend `apiClient`).
 * - No-op when `BUILD_ID === "dev"` (local dev never has a deployed id to
 *   compare against — avoids false positives).
 * - Re-checks on mount, on window focus, on tab re-visibility, and every 5 min.
 * - Concurrent triggers coalesce: alt-tab fires BOTH `focus` and
 *   `visibilitychange`, so an in-flight check short-circuits duplicates rather
 *   than double-fetching `/version`.
 * - Network errors are swallowed: a failed fetch must never flip the flag to
 *   `true` (don't nag users on a transient blip).
 */
export function useVersionCheck(): { updateAvailable: boolean } {
  const [updateAvailable, setUpdateAvailable] = useState(false);
  // Guards against double-fetching when multiple triggers fire close together
  // (e.g. alt-tab emits both 'focus' and 'visibilitychange').
  const inFlightRef = useRef(false);

  const check = useCallback(async () => {
    if (BUILD_ID === "dev") return;
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const res = await fetch("/version", { cache: "no-store" });
      if (!res.ok) return;
      const data: { buildId?: string } = await res.json();
      if (data?.buildId && data.buildId !== BUILD_ID) {
        setUpdateAvailable(true);
      }
    } catch {
      // Swallow — never flip the flag on a failed fetch.
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  useEffect(() => {
    if (BUILD_ID === "dev") return;

    void check();

    const onFocus = () => void check();
    const onVisibility = () => {
      if (document.visibilityState === "visible") void check();
    };

    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    const interval = window.setInterval(() => void check(), CHECK_INTERVAL_MS);

    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearInterval(interval);
    };
  }, [check]);

  return { updateAvailable };
}
