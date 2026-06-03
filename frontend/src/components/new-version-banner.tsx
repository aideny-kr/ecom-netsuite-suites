"use client";

import { useState } from "react";
import { RefreshCw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useVersionCheck } from "@/hooks/use-version-check";

/**
 * Non-blocking bottom banner shown when a newer frontend build is deployed
 * while this tab is still running the old bundle. User-initiated recovery:
 * the user clicks Refresh (a full reload) to pull the new version, or dismisses
 * it. Dismissing only hides it for this render; a later check can re-surface it.
 */
export function NewVersionBanner() {
  const { updateAvailable } = useVersionCheck();
  const [dismissed, setDismissed] = useState(false);

  if (!updateAvailable || dismissed) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-4 left-1/2 z-[100] flex -translate-x-1/2 items-center gap-3 rounded-xl border border-blue-500/30 bg-blue-500/10 px-4 py-2.5 shadow-soft backdrop-blur"
    >
      <span className="text-[13px] text-foreground">
        A new version is available.
      </span>
      <Button
        variant="outline"
        size="sm"
        onClick={() => window.location.reload()}
      >
        <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
        Refresh
      </Button>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => setDismissed(true)}
        className="inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
