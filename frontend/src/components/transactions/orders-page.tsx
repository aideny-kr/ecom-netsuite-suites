"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef, SortingState } from "@tanstack/react-table";
import { RefreshCw, SearchCheck } from "lucide-react";
import { useConnections } from "@/hooks/use-connections";
import { useTableData } from "@/hooks/use-table-data";
import {
  useTransactionAccess,
  useTransactionConfigs,
} from "@/hooks/use-transaction-ops";
import { apiClient } from "@/lib/api-client";
import { DataTable } from "@/components/data-table";
import { TableToolbar } from "@/components/table-toolbar";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { transactionColumns } from "./columns";
import { transactionAmount, transactionDate } from "./format";
import { investigationChatLink } from "@/components/transaction-ops/agent-link";

type Metric = {
  source: string | null;
  target: string | null;
  delta: string | null;
};
type Balance = {
  currency?: string;
  target_currency?: string;
  amounts?: Record<string, Metric>;
};
type Evidence = {
  status: string;
  stale?: boolean;
  checked_at?: string;
  run_id?: string;
  balance?: Balance;
};
type Order = Record<string, unknown> & {
  id: string;
  order_number: string;
  currency: string;
  reconciliation: Evidence;
};
type SyncStatus = {
  status: string;
  records_imported: number;
  last_completed_at?: string;
  coverage_since?: string;
};
type Run = { id: string; config_snapshot: { name: string } };
const labels: Record<string, string> = {
  matched: "Matched",
  missing_in_netsuite: "Missing in NetSuite",
  difference: "Needs review",
  ambiguous: "Multiple matches",
  currency_mismatch: "Currency differs",
  incomplete: "Not verified",
  not_verified: "Not verified",
};
const tabs = [
  ["", "All orders"],
  ["needs_review", "Needs review"],
  ["missing_in_netsuite", "Missing in NetSuite"],
  ["not_verified", "Not verified"],
  ["matched", "Matched"],
];
const inputClass = "h-10 rounded-md border bg-background px-3 text-[13px]";
const errorMessage = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The request could not be completed. Please retry.";
function checkedAt(value?: string) {
  return value && Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString()
    : "Not checked yet";
}
function BalanceStatus({ status }: { status: string }) {
  const tone =
    status === "matched"
      ? "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200"
      : [
            "difference",
            "ambiguous",
            "currency_mismatch",
            "missing_in_netsuite",
          ].includes(status)
        ? "bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-200"
        : "bg-muted text-muted-foreground";
  return (
    <span
      className={`inline-flex whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-medium ${tone}`}
    >
      {labels[status] || "Not verified"}
    </span>
  );
}

export function OrdersPage() {
  const router = useRouter();
  const access = useTransactionAccess();
  const client = useQueryClient();
  const connections = useConnections();
  const configs = useTransactionConfigs();
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const [sourceId, setSourceId] = useState("");
  const sources = (connections.data || []).filter(
    (c) =>
      String(c.provider) === "solidus" &&
      c.metadata_json?.api_profile === "framework_sync",
  );
  const source = sources.find((c) => c.id === sourceId) || sources[0];
  const sourceIdentity = useRef({ id: source?.id, version: 0 });
  if (sourceIdentity.current.id !== source?.id)
    sourceIdentity.current = {
      id: source?.id,
      version: sourceIdentity.current.version + 1,
    };
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [sorting, setSorting] = useState<SortingState>([
    { id: "source_created_at", desc: true },
  ]);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [currency, setCurrency] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [selected, setSelected] = useState<Order | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const requestKeys = useRef(new Map<string, string>());
  const reconciliationRequest = useRef<{
    source_connection_id: string;
    evaluation_key: string;
    window_start: string;
    window_end: string;
  }>();
  const filters: Record<string, string> = {};
  if (source) filters.source_connection_id = source.id;
  if (status) filters.reconciliation_status = status;
  if (currency) filters.currency = currency;
  if (dateFrom) filters.date_from = `${dateFrom}T00:00:00Z`;
  if (dateTo)
    filters.date_to = new Date(
      Date.parse(`${dateTo}T00:00:00Z`) + 86400000,
    ).toISOString();
  const table = useTableData<Order>({
    tableName: "orders",
    page,
    pageSize,
    search,
    filters,
    sortBy: sorting[0]?.id,
    sortOrder: sorting[0]?.desc ? "desc" : "asc",
    refetchInterval: 10000,
  });
  const sync = useQuery({
    queryKey: ["transaction-source", access.tenantId, source?.id],
    enabled: !!source && !!access.tenantId,
    queryFn: () =>
      apiClient.get<SyncStatus>(
        `/api/v1/connections/${encodeURIComponent(source!.id)}/sync-status`,
      ),
    refetchInterval: 5000,
  });
  const refreshing = ["queued", "running"].includes(sync.data?.status || "");
  const refresh = useMutation({
    mutationFn: () =>
      apiClient.post(
        `/api/v1/connections/${encodeURIComponent(source!.id)}/sync`,
        {},
      ),
    onSuccess: () =>
      client.invalidateQueries({
        queryKey: ["transaction-source", access.tenantId],
      }),
  });
  const reconcile = useMutation({
    mutationFn: (request: { sourceId: string; version: number }) => {
      if (
        reconciliationRequest.current?.source_connection_id !== request.sourceId
      ) {
        const end = new Date();
        reconciliationRequest.current = {
          source_connection_id: request.sourceId,
          evaluation_key: crypto.randomUUID(),
          window_start: new Date(end.getTime() - 86400000).toISOString(),
          window_end: end.toISOString(),
        };
      }
      return apiClient.post<Run[]>(
        "/api/v1/transaction-ops/reconcile",
        reconciliationRequest.current,
      );
    },
    onSuccess: (result, request) => {
      if (
        !mounted.current ||
        request.version !== sourceIdentity.current.version
      )
        return;
      setRuns(result);
      reconciliationRequest.current = undefined;
      client.invalidateQueries({
        queryKey: ["transaction-ops", access.tenantId],
      });
    },
  });
  const investigate = useMutation({
    mutationFn: ({
      order,
    }: {
      order: Order;
      chat: boolean;
      version: number;
    }) => {
      if (!requestKeys.current.has(order.id))
        requestKeys.current.set(order.id, crypto.randomUUID());
      return apiClient.post<Run>(
        `/api/v1/transaction-ops/orders/${encodeURIComponent(order.id)}/investigate`,
        { evaluation_key: requestKeys.current.get(order.id) },
      );
    },
    onSuccess: (run, request) => {
      if (mounted.current && request.version === sourceIdentity.current.version)
        router.push(
          request.chat
            ? investigationChatLink(run.id)
            : `/transaction-operations/runs/${encodeURIComponent(run.id)}`,
        );
    },
  });
  const resetInvestigation = investigate.reset;
  const columns = useMemo<ColumnDef<Record<string, unknown>, unknown>[]>(() => {
    const base = transactionColumns("orders").filter((c) => c.id !== "status");
    base[0] = {
      id: "order_number",
      accessorKey: "order_number",
      header: "Order number",
      cell: ({ row }) => (
        <button
          className="font-medium text-primary hover:underline"
          aria-label={`Open order ${row.original.order_number}`}
          onClick={() => {
            resetInvestigation();
            setSelected(row.original as Order);
          }}
        >
          {String(row.original.order_number)}
        </button>
      ),
    };
    return [
      ...base,
      {
        id: "refunds",
        header: () => (
          <span className="block text-right">Completed refunds</span>
        ),
        enableSorting: false,
        cell: ({ row }) => (
          <span className="block text-right font-mono tabular-nums">
            {transactionAmount(
              (row.original.reconciliation as Evidence)?.balance?.amounts
                ?.refunds?.source,
              row.original.currency,
            )}
          </span>
        ),
      },
      {
        id: "reconciliation",
        header: "Reconciliation",
        enableSorting: false,
        cell: ({ row }) => (
          <BalanceStatus
            status={(row.original.reconciliation as Evidence)?.status}
          />
        ),
      },
    ];
  }, [resetInvestigation]);
  const sourceConfigs =
    configs.data?.filter((c) => c.source_connection_id === source?.id) || [];
  const daily =
    sourceConfigs.length > 0 &&
    sourceConfigs.every(
      (c) => c.enabled && c.schedule_enabled && c.interval_minutes === 1440,
    );
  const failure = refresh.error || reconcile.error;
  return (
    <div className="animate-fade-in space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="mb-2 text-[13px] text-muted-foreground">Transactions</p>
          <h1 className="text-2xl font-semibold tracking-tight">Orders</h1>
          <p className="mt-2 text-[15px] text-muted-foreground">
            Compare order totals, VAT / tax, and completed refunds with
            NetSuite.
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            disabled={
              !source ||
              !access.canManage ||
              refreshing ||
              refresh.isPending ||
              source.status !== "active"
            }
            onClick={() => refresh.mutate()}
          >
            <RefreshCw className="mr-2 h-4 w-4" />
            {refreshing || refresh.isPending ? "Refreshing…" : "Refresh data"}
          </Button>
          <Button
            disabled={
              !source ||
              !access.allowed ||
              reconcile.isPending ||
              source.status !== "active"
            }
            onClick={() =>
              reconcile.mutate({
                sourceId: source!.id,
                version: sourceIdentity.current.version,
              })
            }
          >
            <SearchCheck className="mr-2 h-4 w-4" />
            {reconcile.isPending ? "Starting…" : "Reconcile now"}
          </Button>
        </div>
      </header>
      <div className="rounded-xl border bg-card px-5 py-4 text-[13px]">
        {sources.length > 1 && (
          <select
            aria-label="Order source"
            value={source?.id}
            className={`${inputClass} mb-3`}
            onChange={(e) => {
              setSourceId(e.target.value);
              setPage(1);
              setSelected(null);
              setRuns([]);
              refresh.reset();
              reconcile.reset();
              investigate.reset();
            }}
          >
            {sources.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>
        )}
        {connections.error ? (
          <p role="alert">
            Connections could not be loaded.{" "}
            <button className="underline" onClick={() => connections.refetch()}>
              Try again
            </button>
          </p>
        ) : source ? (
          <div className="flex flex-wrap justify-between gap-3">
            <div>
              <span className="font-medium">{source.label}</span>
              <span className="mx-2 text-muted-foreground">·</span>
              {sync.data?.records_imported ?? "—"} orders imported
              <p className="mt-1 text-muted-foreground">
                {sync.error
                  ? "Refresh status unavailable"
                  : refreshing
                    ? "Import in progress. Orders appear as they arrive."
                    : sync.data?.last_completed_at
                      ? `Last completed refresh ${checkedAt(sync.data.last_completed_at)}`
                      : "Refresh data to import orders updated in the last 7 days."}
              </p>
              {["partial", "failed", "delayed"].includes(
                sync.data?.status || "",
              ) && (
                <p className="mt-1 text-amber-700">
                  Refresh {sync.data?.status}. Imported orders are retained; use
                  Refresh data to continue.
                </p>
              )}
            </div>
            <div className="text-muted-foreground">
              <p>
                {daily
                  ? "Daily reconciliation enabled"
                  : sourceConfigs.length
                    ? "Custom or paused reconciliation schedule"
                    : "Daily checks start with your first reconciliation"}
              </p>
              <p className="mt-1">
                Checks orders and refunds changed in the last 24 hours. Open any
                order to investigate it on demand.
              </p>
            </div>
          </div>
        ) : (
          <p>
            {connections.isLoading ? (
              "Loading sources…"
            ) : (
              <>
                Connect Solidus to import orders.{" "}
                <Link className="text-primary underline" href="/connections">
                  Open Connections
                </Link>
              </>
            )}
          </p>
        )}
        {failure && (
          <p role="alert" className="mt-3 text-destructive">
            {errorMessage(failure)}
          </p>
        )}
        {runs.length > 0 && (
          <div role="status" className="mt-3 border-t pt-3">
            Investigations queued.{" "}
            {runs.map((run) => (
              <Link
                key={run.id}
                className="mr-4 inline-block text-primary underline"
                href={`/transaction-operations/runs/${encodeURIComponent(run.id)}`}
              >
                {run.config_snapshot.name}
              </Link>
            ))}
          </div>
        )}
      </div>
      <nav
        aria-label="Reconciliation status"
        className="flex flex-wrap gap-1 border-b"
      >
        {tabs.map(([value, label]) => (
          <button
            key={value}
            aria-pressed={status === value}
            className={`border-b-2 px-4 py-3 text-[13px] font-medium ${status === value ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground"}`}
            onClick={() => {
              setStatus(value);
              setPage(1);
            }}
          >
            {label}
          </button>
        ))}
      </nav>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <TableToolbar
          tableName="orders"
          search={search}
          filters={filters}
          onSearchChange={(value) => {
            setSearch(value);
            setPage(1);
          }}
        />
        <div className="flex flex-wrap items-end gap-3">
          <label className="space-y-1 text-xs text-muted-foreground">
            <span className="block">Currency</span>
            <select
              aria-label="Currency"
              className={inputClass}
              value={currency}
              onChange={(e) => {
                setCurrency(e.target.value);
                setPage(1);
              }}
            >
              <option value="">All currencies</option>
              {[
                "AUD",
                "CAD",
                "CHF",
                "EUR",
                "GBP",
                "JPY",
                "NZD",
                "SGD",
                "TWD",
                "USD",
              ].map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </label>
          <label className="space-y-1 text-xs text-muted-foreground">
            <span className="block">Order date from (UTC)</span>
            <input
              aria-label="Order date from"
              className={inputClass}
              type="date"
              value={dateFrom}
              onChange={(e) => {
                setDateFrom(e.target.value);
                setPage(1);
              }}
            />
          </label>
          <label className="space-y-1 text-xs text-muted-foreground">
            <span className="block">Through (UTC)</span>
            <input
              aria-label="Order date through"
              className={inputClass}
              type="date"
              value={dateTo}
              onChange={(e) => {
                setDateTo(e.target.value);
                setPage(1);
              }}
            />
          </label>
        </div>
      </div>
      {table.error ? (
        <div role="alert" className="rounded-xl border p-6">
          Orders could not be loaded.{" "}
          <p className="mt-2 text-muted-foreground">{table.error.message}</p>
          <Button
            variant="outline"
            className="mt-4"
            onClick={() => table.refetch()}
          >
            Try again
          </Button>
        </div>
      ) : table.isLoading ? (
        <Skeleton className="h-64 w-full rounded-xl" />
      ) : (
        <>
          <p className="text-xs text-muted-foreground">
            {table.data?.total ?? 0} orders · Open an order to compare evidence
            and investigate.
          </p>
          <DataTable
            columns={columns}
            data={table.data?.items || []}
            page={page}
            pageSize={pageSize}
            totalPages={table.data?.pages || 1}
            sorting={sorting}
            onSortingChange={(value) => {
              setSorting(value);
              setPage(1);
            }}
            onPageChange={setPage}
            onPageSizeChange={(value) => {
              setPageSize(value);
              setPage(1);
            }}
            onRowClick={(row) => {
              investigate.reset();
              setSelected(row as Order);
            }}
          />
          {table.data?.total === 0 &&
            !search &&
            !status &&
            !currency &&
            !dateFrom &&
            !dateTo && (
              <p className="rounded-xl border border-dashed p-8 text-center text-muted-foreground">
                Your orders will appear here after the first data refresh.
              </p>
            )}
        </>
      )}
      <Dialog
        open={!!selected}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      >
        <DialogContent className="left-auto right-0 top-0 h-dvh max-w-2xl translate-x-0 translate-y-0 overflow-y-auto rounded-none p-8">
          {selected && (
            <div className="space-y-7">
              <DialogHeader>
                <p className="text-xs text-muted-foreground">
                  Order comparison
                </p>
                <DialogTitle className="text-2xl">
                  {selected.order_number}
                </DialogTitle>
                <DialogDescription>
                  {transactionDate(selected.source_created_at)} ·{" "}
                  {selected.currency}
                </DialogDescription>
              </DialogHeader>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <BalanceStatus status={selected.reconciliation?.status} />
                <p className="text-xs text-muted-foreground">
                  Last check: {checkedAt(selected.reconciliation?.checked_at)}
                </p>
              </div>
              {selected.reconciliation?.stale && (
                <p className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900">
                  This comparison needs a fresh check. The evidence below is
                  from the previous investigation.
                </p>
              )}
              <div className="overflow-x-auto">
                <table className="w-full text-[13px]">
                  <thead>
                    <tr className="border-b text-xs text-muted-foreground">
                      <th className="py-3 text-left font-medium">Amount</th>
                      <th className="text-right font-medium">Solidus</th>
                      <th className="text-right font-medium">NetSuite</th>
                      <th className="text-right font-medium">Difference</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[
                      ["order_total", "Order total", selected.total_amount],
                      ["tax", "VAT / tax", selected.tax_amount],
                      ["refunds", "Completed refunds", null],
                    ].map(([key, label, fallback]) => {
                      const metric =
                        selected.reconciliation?.balance?.amounts?.[
                          String(key)
                        ];
                      const cur =
                        selected.reconciliation?.balance?.currency ||
                        selected.currency;
                      return (
                        <tr key={String(key)} className="border-b">
                          <th className="py-5 text-left font-medium">
                            {String(label)}
                          </th>
                          <td className="text-right font-mono tabular-nums">
                            {transactionAmount(
                              metric ? metric.source : fallback,
                              cur,
                            )}
                          </td>
                          <td className="text-right font-mono tabular-nums">
                            {transactionAmount(
                              metric?.target,
                              selected.reconciliation?.balance
                                ?.target_currency || cur,
                            )}
                          </td>
                          <td className="text-right font-mono tabular-nums">
                            {transactionAmount(metric?.delta, cur)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="text-[13px] leading-6 text-muted-foreground">
                Unknown amounts appear as —. Order total already includes tax.
                Completed refunds are compared separately. A match requires all
                three amounts and the currency to agree.
              </p>
              <div className="rounded-xl border p-5">
                <h3 className="font-medium">Investigate and resolve</h3>
                <p className="mt-2 text-[13px] leading-6 text-muted-foreground">
                  Check the connected sources, trace the difference, and review
                  a proposed solution. Any supported sync or NetSuite correction
                  requires your approval of the exact change.
                </p>
                <div className="mt-4 flex flex-wrap gap-3">
                  <Button
                    disabled={!access.allowed || investigate.isPending}
                    onClick={() =>
                      investigate.mutate({
                        order: selected,
                        chat: false,
                        version: sourceIdentity.current.version,
                      })
                    }
                  >
                    {investigate.isPending
                      ? "Starting investigation…"
                      : "Investigate order"}
                  </Button>
                  <Button
                    variant="outline"
                    disabled={!access.allowed || investigate.isPending}
                    onClick={() =>
                      investigate.mutate({
                        order: selected,
                        chat: true,
                        version: sourceIdentity.current.version,
                      })
                    }
                  >
                    Work with agent
                  </Button>
                  {selected.reconciliation?.run_id && (
                    <Button asChild variant="outline">
                      <Link
                        href={`/transaction-operations/runs/${encodeURIComponent(selected.reconciliation.run_id)}`}
                      >
                        View previous investigation
                      </Link>
                    </Button>
                  )}
                </div>
                {investigate.error && (
                  <p role="alert" className="mt-3 text-[13px] text-destructive">
                    {errorMessage(investigate.error)}
                  </p>
                )}
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
