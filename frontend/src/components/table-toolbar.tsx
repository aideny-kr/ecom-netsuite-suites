"use client";

import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Download, Search } from "lucide-react";

interface TableToolbarProps {
  tableName: string;
  search: string;
  onSearchChange: (value: string) => void;
}

export function TableToolbar({
  tableName,
  search,
  onSearchChange,
}: TableToolbarProps) {
  function handleExport() {
    const token = localStorage.getItem("access_token");
    const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
    const url = `${baseUrl}/api/v1/tables/${tableName}/export/csv`;

    const link = document.createElement("a");
    link.href = url;
    if (token) {
      // Use fetch for authenticated download
      fetch(url, {
        headers: { Authorization: `Bearer ${token}` },
      })
        .then((res) => res.blob())
        .then((blob) => {
          const blobUrl = URL.createObjectURL(blob);
          link.href = blobUrl;
          link.download = `${tableName}.csv`;
          link.click();
          URL.revokeObjectURL(blobUrl);
        });
    } else {
      link.download = `${tableName}.csv`;
      link.click();
    }
  }

  return (
    <div className="flex items-center gap-4">
      <div className="relative flex-1 max-w-sm">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="Search..."
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          className="pl-9"
        />
      </div>
      <Button variant="outline" onClick={handleExport}>
        <Download className="mr-2 h-4 w-4" />
        Export CSV
      </Button>
    </div>
  );
}
