"use client";

import { useState, useMemo, useCallback } from "react";
import { cn } from "@/lib/utils";
import type { DataTableData } from "@/lib/chat-stream";
import {
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  Copy,
  Check,
  Download,
  FileSpreadsheet,
  Bookmark,
  Loader2,
  Pencil,
  X,
  ChevronDown,
  ChevronUp,
  Code2,
} from "lucide-react";
import {
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCreateSavedQuery } from "@/hooks/use-saved-queries";
import { useExcelExport } from "@/hooks/use-excel-export";

interface DataFrameTableProps {
  data: DataTableData;
  queryText?: string;
}

type SortDirection = "asc" | "desc" | null;

export function DataFrameTable({ data, queryText }: DataFrameTableProps) {
  const { columns, rows, row_count, truncated } = data;
  const { exportToExcel, exportFromQuery, isExporting } = useExcelExport();
  const [sortCol, setSortCol] = useState<number | null>(null);
  const [sortDir, setSortDir] = useState<SortDirection>(null);
  const [copied, setCopied] = useState(false);
  const [showQuery, setShowQuery] = useState(false);

  const handleSort = useCallback(
    (colIndex: number) => {
      if (sortCol === colIndex) {
        setSortDir((d) => (d === "asc" ? "desc" : d === "desc" ? null : "asc"));
        if (sortDir === "desc") setSortCol(null);
      } else {
        setSortCol(colIndex);
        setSortDir("asc");
      }
    },
    [sortCol, sortDir],
  );

  const sortedRows = useMemo(() => {
    if (sortCol === null || sortDir === null) return rows;
    return [...rows].sort((a, b) => {
      const aVal = (a as unknown[])[sortCol];
      const bVal = (b as unknown[])[sortCol];
      if (aVal == null && bVal == null) return 0;
      if (aVal == null) return 1;
      if (bVal == null) return -1;
      const aNum = typeof aVal === "number" ? aVal : Number(aVal);
      const bNum = typeof bVal === "number" ? bVal : Number(bVal);
      if (!isNaN(aNum) && !isNaN(bNum)) {
        return sortDir === "asc" ? aNum - bNum : bNum - aNum;
      }
      const aStr = String(aVal);
      const bStr = String(bVal);
      return sortDir === "asc" ? aStr.localeCompare(bStr) : bStr.localeCompare(aStr);
    });
  }, [rows, sortCol, sortDir]);

  const handleCopy = useCallback(() => {
    const header = columns.join("\t");
    const body = sortedRows
      .map((row) => (row as unknown[]).map((v) => v ?? "").join("\t"))
      .join("\n");
    navigator.clipboard.writeText(`${header}\n${body}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [columns, sortedRows]);

  const handleDownloadCSV = useCallback(() => {
    const escape = (v: unknown) => {
      const s = String(v ?? "");
      return s.includes(",") || s.includes('"') || s.includes("\n")
        ? `"${s.replace(/"/g, '""')}"`
        : s;
    };
    const header = columns.map(escape).join(",");
    const body = rows
      .map((row) => (row as unknown[]).map(escape).join(","))
      .join("\n");
    const blob = new Blob([`${header}\n${body}`], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `query-results-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }, [columns, rows]);

  if (columns.length === 0) return null;

  return (
    <div className="my-3 overflow-hidden rounded-xl border bg-card shadow-soft">
      {/* Header */}
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10">
            <svg
              className="h-3.5 w-3.5 text-primary"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M3 10h18M3 14h18M3 6h18M3 18h18"
              />
            </svg>
          </div>
          <div>
            <p className="text-[13px] font-semibold text-foreground">
              Query Results
            </p>
            <p className="text-[11px] text-muted-foreground">
              {row_count} row{row_count !== 1 ? "s" : ""}
              {truncated ? " (truncated)" : ""} · {columns.length} column
              {columns.length !== 1 ? "s" : ""}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={handleCopy}
            className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            title="Copy to clipboard (tab-separated for Excel)"
          >
            {copied ? (
              <Check className="h-3 w-3 text-green-500" />
            ) : (
              <Copy className="h-3 w-3" />
            )}
            {copied ? "Copied" : "Copy"}
          </button>
          <button
            onClick={handleDownloadCSV}
            className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            title="Download as CSV"
          >
            <Download className="h-3 w-3" />
            CSV
          </button>
          <button
            onClick={() => {
              if (truncated && queryText) {
                exportFromQuery({
                  queryText,
                  title: queryText.slice(0, 80),
                });
              } else {
                exportToExcel({
                  columns,
                  rows: rows as unknown[][],
                  title: queryText?.slice(0, 80) ?? "Query Results",
                });
              }
            }}
            disabled={isExporting}
            className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
            title="Export as Excel"
          >
            {isExporting ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <FileSpreadsheet className="h-3 w-3" />
            )}
            Excel
          </button>
        </div>
      </div>

      {/* Query */}
      {queryText && (
        <div className="border-b">
          <button
            onClick={() => setShowQuery((v) => !v)}
            className="flex w-full items-center gap-1.5 px-4 py-2 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            <Code2 className="h-3 w-3" />
            SuiteQL Query
            {showQuery ? (
              <ChevronUp className="ml-auto h-3 w-3" />
            ) : (
              <ChevronDown className="ml-auto h-3 w-3" />
            )}
          </button>
          {showQuery && (
            <pre className="overflow-x-auto bg-muted/50 px-4 py-3 text-[11px] leading-relaxed text-foreground">
              <code>{queryText}</code>
            </pre>
          )}
        </div>
      )}

      {/* Table */}
      <div className="max-h-[600px] overflow-auto scrollbar-thin">
        <table className="w-max min-w-full caption-bottom text-sm">
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              {columns.map((col, i) => (
                <TableHead
                  key={col}
                  className="sticky top-0 z-10 h-auto cursor-pointer select-none whitespace-nowrap bg-muted/80 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur transition-colors hover:text-foreground"
                  onClick={() => handleSort(i)}
                >
                  <span className="inline-flex items-center gap-1">
                    {toReadableHeader(col)}
                    {sortCol === i && sortDir === "asc" && (
                      <ArrowUp className="h-3 w-3" />
                    )}
                    {sortCol === i && sortDir === "desc" && (
                      <ArrowDown className="h-3 w-3" />
                    )}
                    {sortCol !== i && (
                      <ArrowUpDown className="h-2.5 w-2.5 opacity-30" />
                    )}
                  </span>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {sortedRows.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={columns.length}
                  className="py-8 text-center text-[13px] text-muted-foreground"
                >
                  No results
                </TableCell>
              </TableRow>
            ) : (
              sortedRows.map((row, ri) => (
                <TableRow key={ri} className="border-b border-border/50">
                  {(row as unknown[]).map((cell, ci) => (
                    <TableCell
                      key={`${ri}-${ci}`}
                      className={cn(
                        "max-w-[320px] whitespace-nowrap px-3 py-2 text-[12px]",
                        isNumeric(cell) && "text-right tabular-nums",
                      )}
                    >
                      {formatCellValue(cell)}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            )}
          </TableBody>
        </table>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between border-t px-4 py-2">
        <p className="text-[11px] text-muted-foreground">
          {truncated
            ? `Showing ${rows.length} of ${row_count} rows`
            : `${row_count} row${row_count === 1 ? "" : "s"} returned`}
        </p>
        {queryText && <SaveQueryButton queryText={queryText} />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Save Query button
// ---------------------------------------------------------------------------

function SaveQueryButton({ queryText }: { queryText: string }) {
  const [mode, setMode] = useState<"idle" | "editing" | "saved">("idle");
  const [name, setName] = useState("");
  const mutation = useCreateSavedQuery();

  const handleSave = () => {
    if (!name.trim() || !queryText.trim()) return;
    mutation.mutate(
      { name: name.trim(), query_text: queryText.trim() },
      { onSuccess: () => setMode("saved") },
    );
  };

  if (mode === "saved") {
    return (
      <div className="flex items-center gap-1.5 text-[11px] font-medium text-green-600 dark:text-green-400">
        <Check className="h-3 w-3" />
        Saved to Analytics
      </div>
    );
  }

  if (mode === "editing") {
    return (
      <div className="flex items-center gap-2">
        <div className="relative min-w-0 flex-1">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Query name..."
            className="w-full rounded-md border bg-background px-2.5 py-1 pr-7 text-[11px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSave();
              if (e.key === "Escape") setMode("idle");
            }}
          />
          <Pencil className="absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground/50" />
        </div>
        <button
          onClick={handleSave}
          disabled={!name.trim() || mutation.isPending}
          className="flex shrink-0 items-center gap-1 rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
        >
          {mutation.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            "Save"
          )}
        </button>
        <button
          onClick={() => setMode("idle")}
          className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground"
        >
          <X className="h-3 w-3" />
        </button>
        {mutation.isError && (
          <span className="truncate text-[11px] text-destructive">
            Failed to save
          </span>
        )}
      </div>
    );
  }

  return (
    <button
      onClick={() => setMode("editing")}
      className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:text-primary"
    >
      <Bookmark className="h-3 w-3" />
      Save to Analytics
    </button>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function toReadableHeader(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function isNumeric(value: unknown): boolean {
  if (typeof value === "number") return true;
  if (
    typeof value === "string" &&
    /^-?\d+\.?\d*([eE][+-]?\d+)?$/.test(value)
  )
    return true;
  return false;
}

function formatCellValue(value: unknown): string {
  if (value == null) return "\u2014";
  if (typeof value === "boolean") return String(value);
  if (typeof value === "number") {
    if (Number.isInteger(value)) return value.toLocaleString();
    return value.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }
  if (typeof value === "string") {
    if (/^-?\d+\.?\d*([eE][+-]?\d+)?$/.test(value) && value.length > 0) {
      const num = Number(value);
      if (!isNaN(num)) {
        if (Number.isInteger(num)) return num.toLocaleString();
        return num.toLocaleString(undefined, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        });
      }
    }
    return value;
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
