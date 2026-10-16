"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTransactionAccess } from "@/hooks/use-transaction-ops";
import { useFeatures } from "@/hooks/use-features";
import { usePermissions } from "@/hooks/use-permissions";
import { CANONICAL_TABLES } from "@/lib/constants";
import { parseTransactionView, type TransactionView } from "./navigation";

const views: { id: TransactionView; label: string }[] = [
  { id: "records", label: "Records" }, { id: "reconcile", label: "Reconcile" },
  { id: "cases", label: "Cases" }, { id: "approvals", label: "Approvals" },
  { id: "history", label: "History" },
];
const linkClass = "rounded-md border border-transparent px-4 py-2.5 text-[13px] text-muted-foreground hover:border-primary/50 hover:bg-accent hover:text-foreground aria-[current=page]:border-primary/50 aria-[current=page]:bg-accent aria-[current=page]:text-foreground";

/** Shared navigation around existing screens; no execution or data contract changes. */
export function TransactionsSection({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const search = useSearchParams();
  const access = useTransactionAccess();
  const { data: features } = useFeatures();
  const { hasPermission } = usePermissions();
  const canMatchPayments = features?.reconciliation === true && hasPermission("recon.run");
  const isPayment = pathname === "/reconciliation";
  const isInvestigation = pathname.startsWith("/transaction-operations");
  const paymentView = parseTransactionView(search.get("view"));
  const active: TransactionView = isPayment ? (paymentView === "approvals" || paymentView === "history" ? paymentView : "reconcile") : isInvestigation ? "cases"
    : pathname === "/tables/orders" ? parseTransactionView(search.get("view")) || (access.allowed ? "reconcile" : "records") : "records";
  const orderHref = (view: TransactionView) => {
    const params = new URLSearchParams(pathname === "/tables/orders" ? search.toString() : "");
    params.set("view", view);
    return `/tables/orders?${params}`;
  };
  const paymentDenied = isPayment && !access.loading && !access.error && !canMatchPayments;

  return (
    <div className="space-y-6">
      <header>
        <p className="orbital-eyebrow">From records to resolution</p>
        <h1 className="mt-2 text-2xl font-medium">Transactions</h1>
        <p className="mt-2 text-[13px] text-muted-foreground">Find records, compare systems, investigate differences and review proposed changes.</p>
      </header>
      <nav aria-label="Transaction sections" className="flex flex-wrap gap-2 border-b pb-3">
        {views.map(view => {
          const available = view.id === "records" || (["reconcile", "approvals", "history"].includes(view.id) ? access.allowed || canMatchPayments : access.allowed);
          const href = ["reconcile", "approvals", "history"].includes(view.id) && canMatchPayments && (isPayment || !access.allowed) ? `/reconciliation?view=${view.id}` : orderHref(view.id);
          return available ? <Link key={view.id} href={href} aria-current={active === view.id ? "page" : undefined} className={linkClass}>{view.label}</Link>
            : <span key={view.id} className="rounded-md px-4 py-2.5 text-[13px] text-muted-foreground/60" aria-disabled="true" title="Requires the corresponding feature and reconciliation access">{view.label}</span>;
        })}
      </nav>
      {active === "records" && <nav aria-label="Record types" className="flex flex-wrap gap-2">
        {CANONICAL_TABLES.map(table => <Link key={table.name} href={table.name === "orders" ? orderHref("records") : `/tables/${table.name}`} aria-current={pathname === `/tables/${table.name}` ? "page" : undefined} className={linkClass}>{table.label}</Link>)}
      </nav>}
      {active === "reconcile" && <nav aria-label="Reconciliation types" className="flex flex-wrap gap-2">
        {access.allowed && <Link href={orderHref("reconcile")} aria-current={!isPayment ? "page" : undefined} className={linkClass}>Order consistency</Link>}
        {canMatchPayments && <Link href="/reconciliation" aria-current={isPayment ? "page" : undefined} className={linkClass}>Payment / deposit matching</Link>}
      </nav>}
      {(active === "approvals" || active === "history") && <nav aria-label={active === "approvals" ? "Approval types" : "History types"} className="flex flex-wrap gap-2">
        {access.allowed && <Link href={orderHref(active)} aria-current={!isPayment ? "page" : undefined} className={linkClass}>{active === "approvals" ? "Order corrections" : "Order reviews"}</Link>}
        {canMatchPayments && <Link href={`/reconciliation?view=${active}`} aria-current={isPayment ? "page" : undefined} className={linkClass}>Payment matching</Link>}
      </nav>}
      {isPayment && (active === "approvals" || active === "history") && <p className="text-[13px] text-muted-foreground">{active === "approvals" ? "Select a reconciliation run below to review its matches, exceptions and approval controls." : "Select a previous reconciliation run below to inspect its results and export its evidence."}</p>}
      {active === "cases" && access.allowed && <nav aria-label="Case views" className="flex flex-wrap gap-2">
        <Link href={orderHref("cases")} aria-current={!isInvestigation ? "page" : undefined} className={linkClass}>Open cases</Link>
        <Link href="/transaction-operations" aria-current={isInvestigation ? "page" : undefined} className={linkClass}>Investigations</Link>
      </nav>}
      {isPayment && (access.loading || access.error || paymentDenied) ? <p role="status" className="orbital-notice">{access.loading ? "Loading reconciliation access…" : access.error ? "Access settings could not be loaded. Refresh to try again." : "Payment matching requires reconciliation to be enabled and reconciliation access for your account."}</p> : children}
    </div>
  );
}
