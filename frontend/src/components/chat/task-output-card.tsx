"use client";

import { Download, FileSpreadsheet, FileText, Check } from "lucide-react";
import { useState } from "react";
import { apiClient } from "@/lib/api-client";

interface TaskOutputCardProps {
  data: {
    sku_count: number;
    currency_count: number;
    output_files: Record<string, string>;
    preview: Record<string, any>[];
    template_mode: boolean;
  };
}

export function TaskOutputCard({ data }: TaskOutputCardProps) {
  const [downloading, setDownloading] = useState<string | null>(null);

  const handleDownload = async (format: string, fileId: string) => {
    setDownloading(format);
    try {
      const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      const token = typeof window !== "undefined" ? localStorage.getItem("access_token") : null;
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const response = await fetch(`${baseUrl}/api/v1/task-files/${fileId}/download`, {
        headers,
        credentials: "include",
      });
      if (!response.ok) throw new Error("Download failed");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = format === "excel" ? "pricing-output.xlsx" : "netsuite-import.csv";
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setDownloading(null);
    }
  };

  return (
    <div className="my-3 overflow-hidden rounded-xl border bg-card shadow-soft">
      {/* Header */}
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-green-500/10">
            <Check className="h-3.5 w-3.5 text-green-500" />
          </div>
          <div>
            <p className="text-[13px] font-semibold text-foreground">
              Conversion Complete
            </p>
            <p className="text-[11px] text-muted-foreground">
              {data.sku_count} SKU{data.sku_count !== 1 ? "s" : ""} × {data.currency_count} currenc{data.currency_count !== 1 ? "ies" : "y"}
              {data.template_mode ? " · Template mode" : " · Default output"}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {Object.entries(data.output_files).map(([format, fileId]) => (
            <button
              key={format}
              onClick={() => handleDownload(format, fileId)}
              disabled={downloading === format}
              className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-primary/10 hover:text-primary disabled:opacity-50"
            >
              {format === "excel" ? <FileSpreadsheet className="h-3 w-3" /> : <FileText className="h-3 w-3" />}
              {format === "excel" ? "Excel" : "NetSuite CSV"}
            </button>
          ))}
        </div>
      </div>

      {/* Preview table */}
      {data.preview.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="border-b bg-muted/50">
                {Object.keys(data.preview[0]).map((key) => (
                  <th key={key} className="px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    {key}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.preview.map((row, i) => (
                <tr key={i} className="border-b border-border/50">
                  {Object.values(row).map((val, j) => (
                    <td key={j} className="px-3 py-2 text-[12px] tabular-nums">
                      {typeof val === "number" ? val.toLocaleString() : String(val)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
