"use client";

import { useEffect, useRef, useState } from "react";
import { FileText, FileSpreadsheet, File } from "lucide-react";

import { useDriveFiles, type DriveFileListItem } from "@/hooks/useDriveFolders";
import { cn } from "@/lib/utils";

interface DriveFileMentionPickerProps {
  open: boolean;
  query: string;
  onSelect: (file: { name: string; url: string }) => void;
  onClose: () => void;
}

function mimeIcon(mime: string) {
  if (mime.includes("spreadsheet")) return FileSpreadsheet;
  if (mime.includes("document")) return FileText;
  return File;
}

export function DriveFileMentionPicker({
  open,
  query,
  onSelect,
  onClose,
}: DriveFileMentionPickerProps) {
  const { data, isLoading } = useDriveFiles(query, open);
  const results = data ?? [];
  const [activeIndex, setActiveIndex] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  // Reset active row when the result set changes so Enter never fires on a
  // stale item after a query mutation.
  useEffect(() => {
    setActiveIndex(0);
  }, [query, results.length]);

  // Auto-focus so keyboard events reach the listbox without the user tabbing.
  useEffect(() => {
    if (open) listRef.current?.focus();
  }, [open]);

  if (!open) return null;

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
      return;
    }
    if (results.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => (i + 1) % results.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => (i - 1 + results.length) % results.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const f = results[activeIndex];
      if (f) onSelect({ name: f.name, url: f.web_view_link });
    }
  }

  const empty = !isLoading && results.length === 0;

  return (
    <div className="absolute bottom-full left-0 z-50 mb-1 w-[360px] rounded-lg border bg-card shadow-lg">
      <div className="border-b px-3 py-1.5 text-[11px] uppercase tracking-wide text-muted-foreground">
        Drive files
      </div>
      <ul
        ref={listRef}
        role="listbox"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        className="max-h-[240px] overflow-auto p-1 focus:outline-none"
      >
        {isLoading && (
          <li className="px-3 py-2 text-[12px] text-muted-foreground">Searching...</li>
        )}
        {empty && (
          <li className="px-3 py-2 text-[12px] text-muted-foreground">
            No Drive files match &quot;{query}&quot;
          </li>
        )}
        {results.map((f: DriveFileListItem, idx) => {
          const Icon = mimeIcon(f.mime_type);
          const isActive = idx === activeIndex;
          return (
            <li key={f.id} role="option" aria-selected={isActive}>
              <button
                type="button"
                onMouseEnter={() => setActiveIndex(idx)}
                onClick={() =>
                  onSelect({ name: f.name, url: f.web_view_link })
                }
                className={cn(
                  "flex w-full items-start gap-2 rounded-md px-3 py-1.5 text-left",
                  isActive ? "bg-accent" : "hover:bg-accent/60",
                )}
              >
                <Icon className="mt-0.5 h-4 w-4 shrink-0 text-blue-600" aria-hidden="true" />
                <div className="flex-1 min-w-0">
                  <p className="truncate text-[13px] font-medium text-foreground">
                    {f.name}
                  </p>
                  <p className="truncate text-[11px] text-muted-foreground">
                    {f.folder_name}
                  </p>
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
