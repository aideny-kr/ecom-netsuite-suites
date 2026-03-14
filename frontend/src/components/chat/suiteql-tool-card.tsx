"use client";

import { useState } from "react";
import { useCreateSavedQuery } from "@/hooks/use-saved-queries";
import { useExcelExport } from "@/hooks/use-excel-export";
import type { ToolCallStep, ToolCallTableResultPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  ChevronDown,
  Database,
  Bookmark,
  Check,
  FileSpreadsheet,
  Loader2,
  X,
  Pencil,
} from "lucide-react";
import { TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface SuiteQLToolCardProps {
  step: ToolCallStep;
  userQuestion?: string;
}

export function SuiteQLToolCard({ step, userQuestion }: SuiteQLToolCardProps) {
  const [showQuery, setShowQuery] = useState(false);
  const [saveMode, setSaveMode] = useState<"idle" | "editing" | "saved">("idle");
  const [name, setName] = useState(userQuestion?.slice(0, 120) ?? "");

  const queryText = (step.params?.query as string) ?? (step.params?.sqlQuery as string) ?? "";
  const resultPayload = getTablePayload(step);
  const hasStructuredRows = !!resultPayload;
  const isError = !hasStructuredRows && !!step.result_summary;
  const isMcpTool = step.tool !== "netsuite_suiteql";
  const toolLabel = isMcpTool ? formatMcpToolName(step.tool) : "SuiteQL Query";

  const mutation = useCreateSavedQuery();
  const { exportToExcel, exportFromQuery, isExporting } = useExcelExport();

  const handleMutationSuccess = () => setSaveMode("saved");

  const handleSave = () => {
    if (!name.trim() || !queryText.trim()) return;
    mutation.mutate(
      {
        name: name.trim(),
        query_text: queryText.trim(),
        result_data: resultPayload
          ? { columns: resultPayload.columns, rows: resultPayload.rows, row_count: resultPayload.row_count }
          : undefined,
      },
      { onSuccess: handleMutationSuccess },
    );
  };

  // Parse row count from result_summary if available
  const rowCountMatch = step.result_summary?.match(/(\d+)\s*rows?/i);
  const rowCount = resultPayload?.row_count ?? (rowCountMatch ? Number(rowCountMatch[1]) : null);

  if (!hasStructuredRows) {
    return (
      <div className="rounded-lg border bg-background/80 text-[12px]">
        <button
          onClick={() => setShowQuery(!showQuery)}
          className="flex w-full items-center gap-2 px-3 py-2 transition-colors hover:bg-accent/50"
        >
          <ChevronDown
            className={cn(
              "h-3 w-3 shrink-0 text-muted-foreground transition-transform duration-200",
              !showQuery && "-rotate-90",
            )}
          />
          <Database className="h-3 w-3 shrink-0 text-primary/70" />
          <span className="font-medium truncate">{toolLabel}</span>
          <span className="ml-auto shrink-0 rounded-md bg-muted px-1.5 py-0.5 text-[11px] tabular-nums text-muted-foreground">
            {step.duration_ms}ms
          </span>
        </button>
        <div
          className={cn(
            "overflow-hidden transition-all duration-200",
            showQuery ? "max-h-[600px]" : "max-h-0",
          )}
        >
          <div className="space-y-2 border-t px-3 py-2">
            <pre className="max-h-[200px] overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-muted/50 px-2.5 py-2 text-[11px] font-mono leading-relaxed text-foreground/90 scrollbar-thin">
              {queryText}
            </pre>
            {step.result_summary && (
              <div className={cn("text-[11px]", isError && "text-destructive")}>
                <span className="text-muted-foreground">Result: </span>
                <span>{step.result_summary}</span>
              </div>
            )}
          </div>
        </div>
        <div className="border-t px-3 py-1.5">
          <SaveQueryBar
            saveMode={saveMode}
            setSaveMode={setSaveMode}
            name={name}
            setName={setName}
            mutation={mutation}
            onSave={handleSave}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-2xl border bg-background/90 text-[12px]" data-testid="suiteql-result-card">
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <Database className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        <div className="min-w-0 flex-1">
          <p className="text-[12px] font-semibold text-foreground">Query Results</p>
          <p className="truncate text-[11px] text-muted-foreground">{step.result_summary}</p>
        </div>
        {rowCount != null && (
          <span className="shrink-0 rounded-md bg-primary/10 px-1.5 py-0.5 text-[11px] font-medium tabular-nums text-primary">
            {rowCount} rows
          </span>
        )}
        <span className="shrink-0 rounded-md bg-muted px-1.5 py-0.5 text-[11px] tabular-nums text-muted-foreground">
          {step.duration_ms}ms
        </span>
      </div>

      <div className="max-h-[60vh] overflow-auto scrollbar-thin">
        <div className="min-w-full">
          <table className="w-max min-w-full caption-bottom text-sm">
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                {resultPayload.columns.map((column) => (
                  <TableHead
                    key={column}
                    className="sticky top-0 z-10 h-auto whitespace-nowrap bg-background/95 px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground backdrop-blur"
                  >
                    {toReadableHeader(column)}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {resultPayload.rows.map((row, rowIndex) => (
                <TableRow key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <TableCell
                      key={`${rowIndex}-${cellIndex}`}
                      className="max-w-[320px] whitespace-nowrap px-3 py-2 text-[12px]"
                    >
                      {formatCellValue(cell)}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </table>
        </div>
      </div>

      <div className="space-y-3 border-t px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-[11px] text-muted-foreground">
            {resultPayload.truncated
              ? `Showing ${resultPayload.rows.length} of ${resultPayload.row_count} rows`
              : `${resultPayload.row_count} row${resultPayload.row_count === 1 ? "" : "s"} returned`}
          </div>
          {queryText && (
            <button
              onClick={() => setShowQuery((open) => !open)}
              className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground"
            >
              <ChevronDown
                className={cn(
                  "h-3 w-3 transition-transform duration-200",
                  !showQuery && "-rotate-90",
                )}
              />
              {showQuery ? "Hide query" : "Show query"}
            </button>
          )}
        </div>

        {showQuery && queryText && (
          <pre className="max-h-[200px] overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-muted/50 px-2.5 py-2 text-[11px] font-mono leading-relaxed text-foreground/90 scrollbar-thin">
            {queryText}
          </pre>
        )}

        <div className="flex items-center gap-3">
          <button
            onClick={() => {
              if (!resultPayload) return;
              if (resultPayload.truncated && queryText) {
                exportFromQuery({
                  queryText,
                  title: userQuestion?.slice(0, 80) ?? toolLabel,
                });
              } else {
                exportToExcel({
                  columns: resultPayload.columns,
                  rows: resultPayload.rows,
                  title: userQuestion?.slice(0, 80) ?? toolLabel,
                });
              }
            }}
            disabled={isExporting || !resultPayload}
            className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground hover:text-primary transition-colors disabled:opacity-50"
          >
            {isExporting ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <FileSpreadsheet className="h-3 w-3" />
            )}
            Export Excel
          </button>
          <SaveQueryBar
            saveMode={saveMode}
            setSaveMode={setSaveMode}
            name={name}
            setName={setName}
            mutation={mutation}
            onSave={handleSave}
          />
        </div>
      </div>
    </div>
  );
}

function SaveQueryBar({
  saveMode,
  setSaveMode,
  name,
  setName,
  mutation,
  onSave,
}: {
  saveMode: "idle" | "editing" | "saved";
  setSaveMode: (mode: "idle" | "editing" | "saved") => void;
  name: string;
  setName: (name: string) => void;
  mutation: ReturnType<typeof useCreateSavedQuery>;
  onSave: () => void;
}) {
  return (
    <>
        {saveMode === "idle" && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setSaveMode("editing");
            }}
            className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground hover:text-primary transition-colors"
          >
            <Bookmark className="h-3 w-3" />
            Save to Analytics
          </button>
        )}

        {saveMode === "editing" && (
          <div className="flex items-center gap-2">
            <div className="relative flex-1 min-w-0">
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Query name..."
                className="w-full rounded-md border bg-background px-2.5 py-1 pr-7 text-[11px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") onSave();
                  if (e.key === "Escape") setSaveMode("idle");
                }}
              />
              <Pencil className="absolute right-2 top-1/2 -translate-y-1/2 h-3 w-3 text-muted-foreground/50" />
            </div>
            <button
              onClick={onSave}
              disabled={!name.trim() || mutation.isPending}
              className="shrink-0 flex items-center gap-1 rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
            >
              {mutation.isPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                "Save"
              )}
            </button>
            <button
              onClick={() => setSaveMode("idle")}
              className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground transition-colors"
            >
              <X className="h-3 w-3" />
            </button>
            {mutation.isError && (
              <span className="text-[11px] text-destructive truncate">
                Failed to save
              </span>
            )}
          </div>
        )}

        {saveMode === "saved" && (
          <div className="flex items-center gap-1.5 text-[11px] font-medium text-green-600 dark:text-green-400">
            <Check className="h-3 w-3" />
            Saved to Analytics
          </div>
        )}
    </>
  );
}

function stripLinksColumn(payload: ToolCallTableResultPayload): ToolCallTableResultPayload {
  const linksIdx = payload.columns.indexOf("links");
  if (linksIdx === -1) return payload;
  return {
    ...payload,
    columns: payload.columns.filter((_, i) => i !== linksIdx),
    rows: payload.rows.map((row) => row.filter((_, i) => i !== linksIdx)),
  };
}

function getTablePayload(step: ToolCallStep): ToolCallTableResultPayload | null {
  const payload = step.result_payload;
  if (payload?.kind === "table") {
    return stripLinksColumn(payload);
  }

  try {
    const parsed = JSON.parse(step.result_summary) as Partial<ToolCallTableResultPayload>;
    if (parsed && Array.isArray(parsed.columns) && Array.isArray(parsed.rows)) {
      return {
        kind: "table",
        columns: parsed.columns,
        rows: parsed.rows as unknown[][],
        row_count: typeof parsed.row_count === "number" ? parsed.row_count : parsed.rows.length,
        truncated: Boolean(parsed.truncated),
        query: typeof parsed.query === "string" ? parsed.query : ((step.params?.query as string) ?? ""),
        limit: typeof parsed.limit === "number" ? parsed.limit : parsed.rows.length,
      };
    }
  } catch {
    return null;
  }

  return null;
}

function toReadableHeader(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function formatMcpToolName(tool: string): string {
  // External MCP tools: ext__{hex}__ns_runReport → "Report"
  const raw = tool.includes("__") ? tool.split("__").pop() ?? tool : tool;
  const LABELS: Record<string, string> = {
    ns_runReport: "Report",
    ns_runSavedSearch: "Saved Search",
    ns_listAllReports: "Report List",
    ns_listSavedSearches: "Saved Search List",
    ns_runCustomSuiteQL: "SuiteQL (MCP)",
    ns_getSuiteQLMetadata: "Metadata",
    ns_getSubsidiaries: "Subsidiaries",
    netsuite_suiteql: "SuiteQL Query",
    netsuite_financial_report: "Financial Report",
  };
  return LABELS[raw] ?? raw.replace(/_/g, " ");
}

function formatCellValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "boolean") return String(value);
  // Handle numbers — avoid scientific notation, use locale formatting
  if (typeof value === "number") {
    if (Number.isInteger(value)) return value.toLocaleString();
    return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  // Handle string values that are numeric (NetSuite returns numbers as strings)
  if (typeof value === "string") {
    // Scientific notation (e.g., "1.23E7") or plain numeric strings (e.g., "2832400", "837140.18")
    if (/^-?\d+\.?\d*([eE][+-]?\d+)?$/.test(value) && value.length > 0) {
      const num = Number(value);
      if (!isNaN(num)) {
        if (Number.isInteger(num)) return num.toLocaleString();
        return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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
