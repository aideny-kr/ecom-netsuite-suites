"use client";

import { useState, useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  SavedQueryResponse,
  PreviewRequest,
  PreviewResponse,
  ExportRequest,
  ExportResponse,
} from "@/types/analytics";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Loader2, Download, CheckCircle2, AlertCircle, FileSpreadsheet } from "lucide-react";
import { useExcelExport } from "@/hooks/use-excel-export";
import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  type ColumnDef,
} from "@tanstack/react-table";

// ---------------------------------------------------------------------------
// Export button states
// ---------------------------------------------------------------------------

type ExportState =
  | { phase: "idle" }
  | { phase: "queuing" }
  | { phase: "polling"; taskId: string }
  | { phase: "done"; fileName: string }
  | { phase: "error"; message: string };

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function QueryPreviewModal({
  query,
  onClose,
}: {
  query: SavedQueryResponse;
  onClose: () => void;
}) {
  const { data: previewData, isLoading } = useQuery<PreviewResponse>({
    queryKey: ["query-preview", query.id],
    queryFn: () =>
      apiClient.post<PreviewResponse>("/api/v1/skills/preview", {
        query_id: query.id,
      } as PreviewRequest),
  });

  const [exportState, setExportState] = useState<ExportState>({ phase: "idle" });
  const { exportToExcel, exportFromQuery, isExporting: isExcelExporting } = useExcelExport();

  // -- Build TanStack Table columns from dynamic column names ----------------
  const columns = useMemo<ColumnDef<unknown[]>[]>(() => {
    if (!previewData?.columns) return [];
    return previewData.columns.map((colName, idx) => ({
      id: colName,
      header: colName,
      accessorFn: (row: unknown[]) => row[idx],
      cell: (info) => {
        const val = info.getValue();
        if (val == null || val === "") return "—";
        if (typeof val === "number") {
          if (Number.isInteger(val)) return val.toLocaleString();
          return val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }
        if (typeof val === "string" && /^-?\d+\.?\d*$/.test(val) && val.length > 0) {
          const num = Number(val);
          if (!isNaN(num)) {
            if (Number.isInteger(num)) return num.toLocaleString();
            return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
          }
        }
        return String(val);
      },
    }));
  }, [previewData?.columns]);

  const table = useReactTable({
    data: previewData?.rows ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  // -- Export flow: trigger → poll → download --------------------------------
  const handleExport = useCallback(async () => {
    setExportState({ phase: "queuing" });

    try {
      const res = await apiClient.post<ExportResponse>(
        "/api/v1/skills/export",
        { query_id: query.id } as ExportRequest,
      );
      const taskId = res.task_id;
      setExportState({ phase: "polling", taskId });

      const poll = async () => {
        try {
          const job = await apiClient.get<{
            status: string;
            result_summary?: { file_name?: string; file_path?: string };
            error_message?: string;
          }>(`/api/v1/jobs/${taskId}`);

          if (job.status === "completed") {
            const fileName = job.result_summary?.file_name;
            if (fileName) {
              setExportState({ phase: "done", fileName });
              window.open(
                `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/v1/exports/${fileName}`,
                "_blank",
              );
            } else {
              setExportState({ phase: "done", fileName: "export.csv" });
            }
          } else if (job.status === "failed") {
            setExportState({
              phase: "error",
              message: job.error_message || "Export failed.",
            });
          } else {
            setTimeout(poll, 2000);
          }
        } catch {
          setExportState({
            phase: "error",
            message: "Lost connection while checking export status.",
          });
        }
      };

      poll();
    } catch {
      setExportState({ phase: "error", message: "Failed to start export." });
    }
  }, [query.id]);

  // -- Derived UI helpers ----------------------------------------------------
  const isExportBusy =
    exportState.phase === "queuing" || exportState.phase === "polling";

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[90vw] w-[1200px] bg-card h-[85vh] flex flex-col">
        {/* ── Header ─────────────────────────────────────────────── */}
        <DialogHeader className="flex-none flex flex-row items-center justify-between gap-4 pb-4 border-b">
          <div className="min-w-0">
            <DialogTitle className="text-foreground truncate">
              {query.name}
            </DialogTitle>
            <DialogDescription className="text-[13px] text-muted-foreground">
              Showing up to 500 rows.{" "}
              {previewData && (
                <span className="tabular-nums">
                  {previewData.row_count} row{previewData.row_count !== 1 && "s"} returned
                  {previewData.truncated && " (truncated)"}.
                </span>
              )}
            </DialogDescription>
          </div>

          {/* Export buttons */}
          <div className="flex items-center gap-2 shrink-0">
            <Button
              onClick={() => {
                const isSnapshot = query.query_text.trimStart().startsWith("--") && query.result_data;
                if (isSnapshot && query.result_data) {
                  exportToExcel({
                    columns: query.result_data.columns,
                    rows: query.result_data.rows as unknown[][],
                    title: query.name,
                  });
                } else {
                  exportFromQuery({
                    queryText: query.query_text,
                    title: query.name,
                    format: "xlsx",
                  });
                }
              }}
              disabled={isExcelExporting}
              variant="outline"
              size="sm"
            >
              {isExcelExporting ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : (
                <FileSpreadsheet className="mr-1.5 h-3.5 w-3.5" />
              )}
              Excel
            </Button>
            <Button
              onClick={handleExport}
              disabled={isExportBusy || exportState.phase === "done"}
              variant="outline"
              size="sm"
            >
              {isExportBusy ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              ) : exportState.phase === "done" ? (
                <CheckCircle2 className="mr-1.5 h-3.5 w-3.5 text-green-500" />
              ) : exportState.phase === "error" ? (
                <AlertCircle className="mr-1.5 h-3.5 w-3.5 text-destructive" />
              ) : (
                <Download className="mr-1.5 h-3.5 w-3.5" />
              )}
              {isExportBusy
                ? "Exporting…"
                : exportState.phase === "done"
                  ? "Exported"
                  : exportState.phase === "error"
                    ? "Retry"
                    : "CSV"}
            </Button>
          </div>
        </DialogHeader>

        {/* ── Error banner ─────────────────────────────────────── */}
        {exportState.phase === "error" && (
          <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-[13px] text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0" />
            {exportState.message}
          </div>
        )}

        {/* ── Data table ───────────────────────────────────────── */}
        <div className="flex-1 overflow-auto mt-4 scrollbar-thin">
          {isLoading ? (
            <div className="flex h-full items-center justify-center">
              <Loader2 className="h-8 w-8 animate-spin text-primary" />
            </div>
          ) : (
            <div className="rounded-lg border h-full overflow-auto scrollbar-thin">
              <table className="w-full text-sm text-left border-collapse">
                <thead className="bg-muted/50 sticky top-0 z-10">
                  {table.getHeaderGroups().map((hg) => (
                    <tr key={hg.id}>
                      {hg.headers.map((header) => (
                        <th
                          key={header.id}
                          className="px-4 py-3 font-semibold text-foreground border-b whitespace-nowrap text-[13px]"
                        >
                          {header.isPlaceholder
                            ? null
                            : flexRender(
                                header.column.columnDef.header,
                                header.getContext(),
                              )}
                        </th>
                      ))}
                    </tr>
                  ))}
                </thead>
                <tbody className="divide-y">
                  {table.getRowModel().rows.map((row) => (
                    <tr
                      key={row.id}
                      className="hover:bg-muted/30 transition-colors"
                    >
                      {row.getVisibleCells().map((cell) => {
                        const rawVal = cell.getValue();
                        const isNum = typeof rawVal === "number" || (typeof rawVal === "string" && /^-?\d+\.?\d*$/.test(rawVal));
                        return (
                        <td
                          key={cell.id}
                          className={`px-4 py-2.5 text-muted-foreground text-[13px] max-w-xs truncate ${isNum ? "text-right tabular-nums" : ""}`}
                        >
                          {
                            flexRender(
                              cell.column.columnDef.cell,
                              cell.getContext(),
                            ) as React.ReactNode
                          }
                        </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
              {previewData?.rows.length === 0 && (
                <div className="py-16 text-center text-muted-foreground text-[13px]">
                  Query returned no rows.
                </div>
              )}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
