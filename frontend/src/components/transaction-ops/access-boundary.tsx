"use client";
import Link from "next/link";
import { useTransactionAccess } from "@/hooks/use-transaction-ops";
import { cardClass } from "./evidence";
export function TransactionAccessBoundary({
  children,
}: {
  children: React.ReactNode;
}) {
  const access = useTransactionAccess();
  if (access.loading)
    return <p role="status">Loading transaction operations…</p>;
  if (access.error)
    return (
      <p role="alert">
        Access settings could not be loaded. Refresh the page to try again.
      </p>
    );
  if (!access.allowed)
    return (
      <div className={`${cardClass} space-y-3`}>
        <h1 className="text-2xl font-semibold">Transaction operations</h1>
        <p className="text-[15px] text-muted-foreground">
          This workspace needs Celigo and reconciliation enabled, with
          reconciliation access for your account.
        </p>
        <Link
          href="/connections"
          className="text-[13px] underline underline-offset-4"
        >
          Open Connections
        </Link>
      </div>
    );
  return <>{children}</>;
}
