import type { ReactNode } from "react";
import { ChevronDown, Plug } from "lucide-react";

/** Collapse presentation only: credential forms stay mounted and retain drafts. */
export function ConnectionGroup({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return (
    <details className="connection-group group rounded-lg border bg-card open:border-primary/45">
      <summary className="flex cursor-pointer list-none items-center gap-4 rounded-lg p-5 transition-colors hover:bg-accent focus-visible:bg-accent [&::-webkit-details-marker]:hidden">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-input text-primary"><Plug size={17} aria-hidden="true" /></span>
        <span className="min-w-0 flex-1"><span className="block text-sm font-medium">{title}</span><span className="mt-1 block text-xs leading-relaxed text-muted-foreground">{description}</span></span>
        <span className="hidden text-xs text-primary sm:block">Manage access</span>
        <ChevronDown size={16} className="shrink-0 text-primary group-open:rotate-180" aria-hidden="true" />
      </summary>
      <div className="border-t p-4 md:p-5">{children}</div>
    </details>
  );
}
