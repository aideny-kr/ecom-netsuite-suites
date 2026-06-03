"use client";

import { useEffect } from "react";
import {
  isStaleChunkError,
  reloadOnceForStaleChunk,
} from "@/lib/recover-from-stale-chunk";

/**
 * Renders nothing. Listens for uncaught errors / promise rejections and, when
 * they look like a stale code-split chunk failure (post-deploy bundle skew),
 * triggers a single guarded reload so the tab self-recovers onto the new build.
 *
 * The reload is loop-guarded inside {@link reloadOnceForStaleChunk}.
 */
export function ChunkReloadGuard() {
  useEffect(() => {
    const onError = (event: ErrorEvent) => {
      if (isStaleChunkError(event.error)) {
        reloadOnceForStaleChunk();
      }
    };

    const onRejection = (event: PromiseRejectionEvent) => {
      if (isStaleChunkError(event.reason)) {
        reloadOnceForStaleChunk();
      }
    };

    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);

    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);

  return null;
}
