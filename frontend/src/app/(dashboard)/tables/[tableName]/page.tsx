"use client";

import { useState, useMemo } from "react";
import { useParams } from "next/navigation";
import { type ColumnDef, type SortingState } from "@tanstack/react-table";
import { useTableData } from "@/hooks/use-table-data";
import { DataTable } from "@/components/data-table";
import { TableToolbar } from "@/components/table-toolbar";
import { Skeleton } from "@/components/ui/skeleton";
import { CANONICAL_TABLES } from "@/lib/constants";
import { RowDetailDrawer } from "@/components/row-detail-drawer";

export default function TablePage() {
  const params = useParams<{ tableName: string }>();
  const tableName = params.tableName;

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [sorting, setSorting] = useState<SortingState>([]);
  const [search, setSearch] = useState("");
  const [selectedRow, setSelectedRow] = useState<Record<string, unknown> | null>(null);

  const sortBy = sorting[0]?.id;
  const sortOrder = sorting[0]?.desc ? "desc" : "asc";

  const { data, isLoading } = useTableData({
    tableName,
    page,
    pageSize,
    sortBy,
    sortOrder: sortBy ? sortOrder : undefined,
    search: search || undefined,
  });

  const tableMeta = CANONICAL_TABLES.find((t) => t.name === tableName);

  const columns = useMemo<ColumnDef<Record<string, unknown>, unknown>[]>(() => {
    if (!data?.items?.length) return [];
    const sampleRow = data.items[0];
    return Object.keys(sampleRow).map((key) => ({
      id: key,
      accessorKey: key,
      header: key
        .replace(/_/g, " ")
        .replace(/\b\w/g, (c) => c.toUpperCase()),
      cell: ({ getValue }) => {
        const val = getValue();
        if (val === null || val === undefined) return "-";
        if (typeof val === "object") return JSON.stringify(val);
        return String(val);
      },
      enableSorting: true,
    }));
  }, [data?.items]);

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">
          {tableMeta?.label || tableName}
        </h2>
        {tableMeta && (
          <p className="mt-1 text-[15px] text-muted-foreground">
            {tableMeta.description}
          </p>
        )}
      </div>

      <TableToolbar
        tableName={tableName}
        search={search}
        onSearchChange={(v) => {
          setSearch(v);
          setPage(1);
        }}
      />

      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-10 w-full rounded-xl" />
          <Skeleton className="h-64 w-full rounded-xl" />
        </div>
      ) : (
        <DataTable
          columns={columns}
          data={data?.items || []}
          page={page}
          totalPages={data?.pages || 1}
          pageSize={pageSize}
          sorting={sorting}
          onSortingChange={setSorting}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(1);
          }}
          onRowClick={(row) => setSelectedRow(row as Record<string, unknown>)}
        />
      )}

      <RowDetailDrawer
        open={selectedRow !== null}
        onOpenChange={(open) => { if (!open) setSelectedRow(null); }}
        row={selectedRow}
        tableName={tableName}
      />
    </div>
  );
}
