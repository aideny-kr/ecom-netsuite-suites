"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, Download } from "lucide-react";

export type ExportSection = "groups" | "proposals" | "results";

export interface ExportMenuParams {
  section: ExportSection;
  group_key?: string;
  currency?: string;
  action?: string;
}

const ENTRIES: { format: "csv" | "xlsx"; label: string }[] = [
  { format: "csv", label: "CSV — visible columns" },
  { format: "xlsx", label: "Excel — formatted sheet" },
];

// Query-string order mirrors the export endpoint contract:
// section, format, then the optional narrowing filters.
function buildHref(runId: string, format: "csv" | "xlsx", params: ExportMenuParams): string {
  const query = [`section=${encodeURIComponent(params.section)}`, `format=${format}`];
  if (params.group_key) query.push(`group_key=${encodeURIComponent(params.group_key)}`);
  if (params.currency) query.push(`currency=${encodeURIComponent(params.currency)}`);
  if (params.action) query.push(`action=${encodeURIComponent(params.action)}`);
  return `/api/v1/reconciliation/runs/${encodeURIComponent(runId)}/export?${query.join("&")}`;
}

interface ExportMenuProps {
  runId: string;
  params: ExportMenuParams;
  className?: string;
}

/** Small "Export" dropdown whose entries are bare `<a href>` links to the
 * export endpoint (same auth story as the Evidence Pack link — no
 * apiClient/fetch/blob for downloads). Simple controlled popover (not the
 * unused Radix ui/dropdown-menu.tsx primitive) — mirrors the outside-click +
 * Escape pattern already used in file-mention-picker.tsx / chat-input.tsx. */
export function ExportMenu({ runId, params, className }: ExportMenuProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <div ref={containerRef} className={`relative inline-block ${className ?? ""}`}>
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[13px] text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Download className="h-3.5 w-3.5" />
        Export
        <ChevronDown className="h-3 w-3" />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-20 mt-1 min-w-[13rem] rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
        >
          {ENTRIES.map((entry) => (
            <a
              key={entry.format}
              role="menuitem"
              href={buildHref(runId, entry.format, params)}
              onClick={() => setOpen(false)}
              className="block rounded-sm px-2 py-1.5 text-[13px] text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:bg-accent"
            >
              {entry.label}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
