"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { apiClient } from "@/lib/api-client";

export interface RefundCheck {
  finding_id: string;
  run_id: string;
  order_reference: string;
  entity: string;
  currency: string;
  source_amount: string | null;
  netsuite_amount: string | null;
  difference: string | null;
  status: "difference" | "not_verified" | "matched";
  historical_order_reconciled: boolean;
  observed_at: string | null;
}
interface RefundChecksResponse {
  items: RefundCheck[];
  total: number;
  has_next: boolean;
}

function amount(value: string | null, currency: string) {
  if (value === null) return "Not verified";
  return `${currency} ${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function RefundChecksTable({ items }: { items: RefundCheck[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[13px]">
        <thead className="border-b bg-muted/40 text-muted-foreground">
          <tr>
            <th className="px-4 py-3 text-left">Order / subsidiary</th>
            <th className="px-4 py-3 text-left">Refund check</th>
            <th className="px-4 py-3 text-right">Source refunds</th>
            <th className="px-4 py-3 text-right">NetSuite refunds</th>
            <th className="px-4 py-3 text-right">Difference</th>
            <th className="px-4 py-3 text-left">Evidence observed</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.finding_id} className="border-b last:border-0">
              <td className="px-4 py-3">
                <Link href={`/transaction-operations/runs/${item.run_id}`} className="font-mono text-primary underline-offset-2 hover:underline">{item.order_reference}</Link>
                <div className="text-xs text-muted-foreground">Subsidiary {item.entity}</div>
              </td>
              <td className="px-4 py-3">
                <span className="rounded-full bg-amber-500/10 px-2 py-1 text-amber-700 dark:text-amber-300">
                  {item.status === "not_verified" ? "Not verified" : "Refund difference"}
                </span>
                {item.historical_order_reconciled && <div className="mt-2 text-xs text-muted-foreground">Order previously reconciled · refund check remains open</div>}
              </td>
              <td className="px-4 py-3 text-right tabular-nums">{amount(item.source_amount, item.currency)}</td>
              <td className="px-4 py-3 text-right tabular-nums">{amount(item.netsuite_amount, item.currency)}</td>
              <td className="px-4 py-3 text-right tabular-nums">{amount(item.difference, item.currency)}</td>
              <td className="px-4 py-3 text-xs text-muted-foreground">
                {item.observed_at ? new Date(item.observed_at).toLocaleString() : "Timestamp unavailable"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RefundChecks({ runId }: { runId: string }) {
  const [page, setPage] = useState(0);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  useEffect(() => { setPage(0); setSearch(""); setSearchInput(""); }, [runId]);
  useEffect(() => {
    const timer = setTimeout(() => { setSearch(searchInput); setPage(0); }, 300);
    return () => clearTimeout(timer);
  }, [searchInput]);
  const params = new URLSearchParams({ limit: "25", offset: String(page * 25), search });
  const query = useQuery<RefundChecksResponse>({
    queryKey: ["recon-refund-checks", runId, page, search],
    queryFn: () => apiClient.get(`/api/v1/reconciliation/runs/${runId}/refund-checks?${params}`),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  return (
    <section className="rounded-xl border bg-card shadow-soft" aria-label="Refund checks">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b p-5">
        <div>
          <h2 className="font-semibold">Refund checks {query.data ? `(${query.data.total})` : ""}</h2>
          <p className="mt-1 max-w-3xl text-[13px] text-muted-foreground">
            Saved exceptions for orders and refund activity in this period. Amounts are cumulative order refunds, including earlier refunds.
            Historical order reconciliation does not hide refund exceptions.
          </p>
        </div>
        <input aria-label="Find refund order" placeholder="Find order…" value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
          className="rounded-lg border bg-background px-3 py-2 text-[13px]" />
      </div>
      {query.isLoading && <p className="p-5 text-[13px] text-muted-foreground">Loading saved refund checks…</p>}
      {query.isError && <div className="p-5 text-[13px]">Refund checks could not be loaded. <button className="text-primary underline" onClick={() => query.refetch()}>Retry</button></div>}
      {query.data && (query.data.items.length ? <RefundChecksTable items={query.data.items} /> :
        <p className="p-5 text-[13px] text-muted-foreground">No unresolved saved refund checks found. This does not certify complete source coverage.</p>)}
      <div className="flex flex-wrap items-center justify-between gap-3 border-t p-4 text-xs text-muted-foreground">
        <span>Saved observations · no live source refresh · <Link href="/settings/ops-status" className="underline">Check coverage</Link></span>
        <div className="flex items-center gap-3">
          <button disabled={page === 0 || query.isFetching} onClick={() => setPage(page - 1)}>Previous</button>
          <span>Page {page + 1}</span>
          <button disabled={!query.data?.has_next || query.isFetching} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      </div>
    </section>
  );
}
