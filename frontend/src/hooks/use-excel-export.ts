"use client";

import { useState, useCallback } from "react";
import { apiClient } from "@/lib/api-client";

interface ExcelExportParams {
  columns: string[];
  rows: unknown[][];
  title?: string;
  metadata?: Record<string, string>;
  columnTypes?: Record<string, string>;
}

interface QueryExportParams {
  queryText: string;
  title?: string;
  format?: "xlsx" | "csv";
  metadata?: Record<string, string>;
  columnTypes?: Record<string, string>;
}

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function safeFilename(title: string, ext: string): string {
  const safe = title.replace(/[^a-zA-Z0-9 _-]/g, "_").slice(0, 50);
  return `${safe || "export"}.${ext}`;
}

export function useExcelExport() {
  const [isExporting, setIsExporting] = useState(false);

  const exportToExcel = useCallback(async (params: ExcelExportParams) => {
    setIsExporting(true);
    try {
      const response = await apiClient.download("/api/v1/exports/excel", {
        columns: params.columns,
        rows: params.rows,
        title: params.title ?? "Query Results",
        metadata: params.metadata,
        column_types: params.columnTypes,
      });
      const blob = await response.blob();
      triggerDownload(blob, safeFilename(params.title ?? "Query Results", "xlsx"));
    } finally {
      setIsExporting(false);
    }
  }, []);

  const exportFromQuery = useCallback(async (params: QueryExportParams) => {
    setIsExporting(true);
    try {
      const fmt = params.format ?? "xlsx";
      const response = await apiClient.download("/api/v1/exports/query-export", {
        query_text: params.queryText,
        title: params.title ?? "Query Results",
        format: fmt,
        metadata: params.metadata,
        column_types: params.columnTypes,
      });
      const blob = await response.blob();
      triggerDownload(blob, safeFilename(params.title ?? "Query Results", fmt));
    } finally {
      setIsExporting(false);
    }
  }, []);

  return { exportToExcel, exportFromQuery, isExporting };
}
