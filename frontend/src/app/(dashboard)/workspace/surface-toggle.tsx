"use client";

import { cn } from "@/lib/utils";

export type WorkspaceSurface = "files" | "celigo";

interface CeligoSurfaceToggleProps {
  surface: WorkspaceSurface;
  onChange: (next: WorkspaceSurface) => void;
  /** The `celigo` feature flag. When false the toggle renders nothing at all,
   *  so the Celigo surface is unreachable rather than merely hidden. */
  enabled: boolean;
}

/**
 * Switches the workspace between its two surfaces.
 *
 * Extracted from `page.tsx` so the surface contract can be tested without
 * mounting the whole workspace (file tree, panels, chat, runs, changesets).
 *
 * `aria-pressed` is set rather than relying on the background colour alone: the
 * active surface decides whether an edit-and-deploy UI or a read-only one is on
 * screen, and a screen-reader user needs that state as much as a sighted one.
 */
export function CeligoSurfaceToggle({ surface, onChange, enabled }: CeligoSurfaceToggleProps) {
  if (!enabled) return null;

  return (
    <div className="flex items-center gap-0.5 rounded border p-0.5" role="group" aria-label="Workspace surface">
      <button
        type="button"
        onClick={() => onChange("files")}
        aria-pressed={surface === "files"}
        className={cn(
          "rounded px-2 py-0.5 text-[11px] transition-colors",
          surface === "files"
            ? "bg-accent text-foreground"
            : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
        )}
      >
        Files
      </button>
      <button
        type="button"
        onClick={() => onChange("celigo")}
        aria-pressed={surface === "celigo"}
        className={cn(
          "rounded px-2 py-0.5 text-[11px] transition-colors",
          surface === "celigo"
            ? "bg-accent text-foreground"
            : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
        )}
      >
        Celigo flows
      </button>
    </div>
  );
}
