"use client";

import { useState, useMemo } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { type SortingState } from "@tanstack/react-table";
import { useTableData } from "@/hooks/use-table-data";
import { DataTable } from "@/components/data-table";
import { TableToolbar } from "@/components/table-toolbar";
import { Skeleton } from "@/components/ui/skeleton";
import { CANONICAL_TABLES } from "@/lib/constants";
import { RowDetailDrawer } from "@/components/row-detail-drawer";
import { transactionColumns } from "@/components/transactions/columns";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/providers/auth-provider";
import { TransactionWorkspace } from "@/components/transactions/workspace";

export default function TablePage() {
  const params = useParams<{ tableName: string }>();
  const tableName = params.tableName;
  const { user } = useAuth();
  const searchParams = useSearchParams();
  const payoutId = tableName === "payout_lines" ? searchParams.get("payout_id") : null;
  if (tableName === "orders") return <TransactionWorkspace key={user?.tenant_id} />;
  return <TableContent key={`${user?.tenant_id}:${tableName}:${payoutId}`} tableName={tableName} payoutId={payoutId} />;
}

function TableContent({ tableName, payoutId }: { tableName: string; payoutId: string | null }) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [sorting, setSorting] = useState<SortingState>([]);
  const [search, setSearch] = useState("");
  const [selectedRow, setSelectedRow] = useState<Record<string, unknown> | null>(null);

  const sortBy = sorting[0]?.id;
  const sortOrder = sorting[0]?.desc ? "desc" : "asc";
  const filters: Record<string, string> = payoutId ? { payout_id: payoutId } : {};

  const { data, isLoading, error, refetch } = useTableData({
    tableName,
    page,
    pageSize,
    sortBy,
    sortOrder: sortBy ? sortOrder : undefined,
    search: search || undefined,
    filters,
  });

  const tableMeta = CANONICAL_TABLES.find((t) => t.name === tableName);

  const columns = useMemo(() => transactionColumns(tableName), [tableName]);

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <p className="mb-2 text-[13px] text-muted-foreground">Transactions</p>
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
        filters={filters}
        onSearchChange={(v) => {
          setSearch(v);
          setPage(1);
        }}
      />

      {error ? (
        <div role="alert" className="rounded-xl border p-6">
          <p className="text-[15px]">Transactions could not be loaded.</p>
          <p className="mt-1 text-[13px] text-muted-foreground">{error.message}</p>
          <Button variant="outline" className="mt-4" onClick={() => refetch()}>Try again</Button>
        </div>
      ) : isLoading ? (
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
          onSortingChange={(value) => { setSorting(value); setPage(1); }}
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
