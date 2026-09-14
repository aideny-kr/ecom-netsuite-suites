"use client";

import { useState } from "react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Download, Search } from "lucide-react";
import { apiClient } from "@/lib/api-client";

interface TableToolbarProps {
  tableName: string;
  search: string;
  onSearchChange: (value: string) => void;
  filters?: Record<string, string>;
}

export function TableToolbar({
  tableName,
  search,
  onSearchChange,
  filters = {},
}: TableToolbarProps) {
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState("");
  async function handleExport() {
    setExporting(true);
    setError("");
    try {
      const params = new URLSearchParams(filters);
      if (search) params.set("search", search);
      const csv = await apiClient.getText(`/api/v1/tables/${tableName}/export/csv?${params}`);
      const blobUrl = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = `${tableName}.csv`;
      link.click();
      URL.revokeObjectURL(blobUrl);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed. Please retry.");
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className="relative flex-1 max-w-sm">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          aria-label="Search transactions"
          placeholder={tableName === "orders" ? "Search order number…" : "Search transactions…"}
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          className="h-10 pl-9 text-[13px]"
        />
      </div>
      <Button
        variant="outline"
        onClick={handleExport}
        disabled={exporting}
        className="text-[13px] font-medium"
      >
        <Download className="mr-2 h-4 w-4" />
        {exporting ? "Exporting…" : "Export CSV"}
      </Button>
      {error && <p role="alert" className="basis-full text-[13px] text-destructive">{error}</p>}
    </div>
  );
}
