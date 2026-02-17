"use client";

import { useState, useEffect, useRef } from "react";
import { File } from "lucide-react";
import { apiClient } from "@/lib/api-client";

interface FileResult {
  file_id: string;
  path: string;
  snippet: string;
}

interface FileMentionPickerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string | null;
  onSelect: (file: FileResult) => void;
  children: React.ReactNode;
}

export function FileMentionPicker({
  open,
  onOpenChange,
  workspaceId,
  onSelect,
  children,
}: FileMentionPickerProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FileResult[]>([]);
  const [loading, setLoading] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) {
      setQuery("");
      setResults([]);
      return;
    }
    // Focus input when opened
    setTimeout(() => inputRef.current?.focus(), 50);
  }, [open]);

  useEffect(() => {
    if (!query || query.length < 2 || !workspaceId) {
      setResults([]);
      return;
    }

    const timer = setTimeout(async () => {
      setLoading(true);
      try {
        const data = await apiClient.get<FileResult[]>(
          `/api/v1/workspaces/${workspaceId}/search?query=${encodeURIComponent(query)}&search_type=filename&limit=10`,
        );
        setResults(data);
      } catch {
        setResults([]);
      } finally {
        setLoading(false);
      }
    }, 150);

    return () => clearTimeout(timer);
  }, [query, workspaceId]);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        onOpenChange(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open, onOpenChange]);

  return (
    <div ref={containerRef} className="relative">
      {children}
      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1 w-[340px] rounded-lg border bg-card shadow-lg">
          <div className="p-2">
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search files..."
              className="w-full rounded-md border bg-background px-3 py-1.5 text-[13px] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
          <div className="max-h-[200px] overflow-auto px-1 pb-1">
            {loading && (
              <p className="px-3 py-2 text-[12px] text-muted-foreground">
                Searching...
              </p>
            )}
            {!loading && query.length >= 2 && results.length === 0 && (
              <p className="px-3 py-2 text-[12px] text-muted-foreground">
                No files found
              </p>
            )}
            {results.map((f) => (
              <button
                key={f.file_id}
                onClick={() => {
                  onSelect(f);
                  onOpenChange(false);
                }}
                className="flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-[12px] hover:bg-accent"
              >
                <File className="h-3 w-3 shrink-0 text-muted-foreground" />
                <span className="truncate font-mono">{f.path}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
