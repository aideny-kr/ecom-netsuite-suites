"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  useTransactionAccess,
  useTransactionConfigs,
  useTransactionRuns,
  useStartTransactionRun,
} from "@/hooks/use-transaction-ops";
import { TransactionAccessBoundary } from "./access-boundary";
import { cardClass, inputClass, Status } from "./evidence";
import { dateLabel, parseRunScope, runState, safeError } from "./format";
import { ScopeControls } from "./scope-controls";

export function TransactionOperationsPage() {
  const { tenantId } = useTransactionAccess();
  return (
    <TransactionAccessBoundary>
      <OperationsContent key={tenantId} />
    </TransactionAccessBoundary>
  );
}
function OperationsContent() {
  const { canManage } = useTransactionAccess();
  const router = useRouter();
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const configs = useTransactionConfigs();
  const startRun = useStartTransactionRun();
  const [selectedId, setSelectedId] = useState("");
  const [mode, setMode] = useState<"references" | "window">("references");
  const [references, setReferences] = useState("");
  const [windowStart, setWindowStart] = useState("");
  const [windowEnd, setWindowEnd] = useState("");
  const [error, setError] = useState("");
  const request = useRef<{ scope: string; key: string } | null>(null);
  const config =
    configs.data?.find((item) => item.id === selectedId) ||
    configs.data?.find((item) => item.enabled) ||
    configs.data?.[0];
  const runs = useTransactionRuns(config?.id || "");
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    if (!config?.enabled) return;
    let scope;
    try {
      scope = parseRunScope(mode, references, windowStart, windowEnd);
    } catch (err) {
      setError((err as Error).message);
      return;
    }
    const identity = JSON.stringify({
      tenant: config.tenant_id,
      config: config.id,
      ...scope,
    });
    if (request.current?.scope !== identity)
      request.current = { scope: identity, key: crypto.randomUUID() };
    try {
      const run = await startRun.mutateAsync({
        configId: config.id,
        evaluation_key: request.current.key,
        ...scope,
      });
      if (!mounted.current) return;
      router.push(`/transaction-operations/runs/${encodeURIComponent(run.id)}`);
    } catch (err) {
      if (!mounted.current) return;
      setError(safeError(err));
    }
  }
  return (
    <div className="animate-fade-in space-y-8 text-[15px]">
      <header>
        <p className="text-[13px] text-muted-foreground">
          Framework → NetSuite
        </p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">
          Transaction operations
        </h1>
        <p className="mt-2 max-w-3xl text-muted-foreground">
          Investigate order differences and track their resolution. Review the
          evidence and exact proposed changes before approving an action.
        </p>
        <Button asChild variant="outline" className="mt-4">
          <Link href="/tables/orders">Open Transactions</Link>
        </Button>
      </header>
      {configs.isLoading ? (
        <p role="status">Loading configured scopes…</p>
      ) : configs.error ? (
        <p role="alert">
          Configured scopes could not be loaded. {safeError(configs.error)}
        </p>
      ) : !config ? (
        <section className={`${cardClass} space-y-4`}>
          <Search className="h-6 w-6 text-muted-foreground" />
          <h2 className="text-lg font-semibold">
            Start with your transactions
          </h2>
          <p className="max-w-2xl text-muted-foreground">
            Open Transactions to refresh your connected orders or start a
            reconciliation. Verified connection settings are applied
            automatically in the backend.
          </p>
          <Button asChild variant="outline">
            <Link href="/connections">Open Connections</Link>
          </Button>
        </section>
      ) : (
        <>
          <section
            className={`${cardClass} space-y-5`}
            aria-labelledby="start-heading"
          >
            <div>
              <h2 id="start-heading" className="text-lg font-semibold">
                Start an investigation
              </h2>
              <p className="mt-1 text-[13px] text-muted-foreground">
                Choose an order or date range. Review any proposed correction
                before it can run.
              </p>
            </div>
            <form onSubmit={submit} className="space-y-5">
              <label className="block space-y-2 text-[13px]">
                Business entity
                <select
                  className={inputClass}
                  value={config.id}
                  onChange={(event) => setSelectedId(event.target.value)}
                  disabled={startRun.isPending}
                >
                  {configs.data?.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                      {!item.enabled ? " (disabled)" : ""}
                    </option>
                  ))}
                </select>
              </label>
              <div className="grid gap-4 rounded-lg bg-muted/40 p-4 text-[13px] sm:grid-cols-3">
                <div>
                  <span className="text-muted-foreground">Destination</span>
                  <p className="mt-1 break-all font-medium">
                    {config.netsuite_account_id} · Subsidiary{" "}
                    {config.subsidiary_id}
                  </p>
                  <p>{config.record_type}</p>
                </div>
                <div>
                  <span className="text-muted-foreground">Resolution</span>
                  <p className="mt-1">
                    Your approval is required before any sync or correction.
                  </p>
                </div>
                <div>
                  <span className="text-muted-foreground">Schedule</span>
                  <p className="mt-1">
                    {config.enabled && config.schedule_enabled
                      ? `Enabled · every ${config.interval_minutes} minutes`
                      : "Not enabled"}
                  </p>
                </div>
              </div>
              <fieldset
                disabled={startRun.isPending || !config.enabled}
                className="space-y-4"
              >
                <legend className="mb-3 text-[13px] font-medium">
                  Investigation scope
                </legend>
                <div className="flex flex-wrap gap-5 text-[13px]">
                  <label className="flex items-center gap-2">
                    <input
                      type="radio"
                      name="scope"
                      checked={mode === "references"}
                      onChange={() => setMode("references")}
                    />
                    Specific orders
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="radio"
                      name="scope"
                      checked={mode === "window"}
                      onChange={() => setMode("window")}
                    />
                    Updated time window
                  </label>
                </div>
                {mode === "references" ? (
                  <label className="block space-y-2 text-[13px]">
                    Full order references
                    <textarea
                      className={inputClass}
                      rows={3}
                      aria-label="Full order references"
                      value={references}
                      maxLength={20200}
                      onChange={(event) => setReferences(event.target.value)}
                      placeholder="R123456789-EU"
                      aria-describedby="references-help"
                    />
                    <span
                      id="references-help"
                      className="block text-muted-foreground"
                    >
                      One full reference per line, including any suffix. Up to
                      200 references; duplicates are removed.
                    </span>
                  </label>
                ) : (
                  <div className="space-y-2">
                    <div className="grid gap-4 sm:grid-cols-2">
                      <label className="space-y-2 text-[13px]">
                        Window start (UTC)
                        <input
                          type="datetime-local"
                          className={inputClass}
                          value={windowStart}
                          onChange={(event) =>
                            setWindowStart(event.target.value)
                          }
                        />
                      </label>
                      <label className="space-y-2 text-[13px]">
                        Window end (UTC)
                        <input
                          type="datetime-local"
                          className={inputClass}
                          value={windowEnd}
                          onChange={(event) => setWindowEnd(event.target.value)}
                        />
                      </label>
                    </div>
                    <p className="text-[13px] text-muted-foreground">
                      Dates are UTC. Select an increasing window of at most 31
                      days.
                    </p>
                  </div>
                )}
              </fieldset>
              {!config.enabled && (
                <p className="text-[13px]">
                  This scope is disabled. An administrator must enable it before
                  new investigations can start.
                </p>
              )}
              {error && (
                <p role="alert" className="rounded-md border p-3 text-[13px]">
                  {error}
                </p>
              )}
              <Button
                type="submit"
                className="bg-foreground text-background hover:bg-foreground/90"
                disabled={startRun.isPending || !config.enabled}
              >
                {startRun.isPending
                  ? "Saving investigation…"
                  : "Start investigation"}
                <ArrowRight className="ml-2 h-4 w-4" />
              </Button>
            </form>
            <ScopeControls config={config} />
          </section>
          <section className="space-y-4">
            <div>
              <h2 className="text-lg font-semibold">Recent investigations</h2>
              <p className="mt-1 text-[13px] text-muted-foreground">
                Latest 100 runs for {config.name}. Opening a run shows its saved
                evidence and approval history.
              </p>
            </div>
            {runs.isLoading ? (
              <p role="status">Loading runs…</p>
            ) : runs.error ? (
              <p role="alert">
                Runs could not be loaded. {safeError(runs.error)}
              </p>
            ) : !runs.data?.length ? (
              <p className={`${cardClass} text-muted-foreground`}>
                No investigations recorded for this scope yet.
              </p>
            ) : (
              <div className={`${cardClass} overflow-x-auto`}>
                <table className="w-full text-[13px]">
                  <thead>
                    <tr className="border-b text-left text-muted-foreground">
                      <th className="p-3 font-medium">Started (UTC)</th>
                      <th className="p-3 font-medium">Origin</th>
                      <th className="p-3 font-medium">State</th>
                      <th className="p-3 text-right font-medium">API calls</th>
                      <th className="p-3">
                        <span className="sr-only">Review run</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.data.map((run) => (
                      <tr key={run.id} className="border-b last:border-0">
                        <td className="p-3 whitespace-nowrap">
                          {dateLabel(run.created_at)}
                        </td>
                        <td className="p-3 capitalize">{run.origin}</td>
                        <td className="p-3">
                          <Status>
                            {runState(run.status, run.termination_reason)}
                          </Status>
                        </td>
                        <td className="p-3 text-right tabular-nums">
                          {run.api_calls_used} / {run.max_api_calls}
                        </td>
                        <td className="p-3">
                          <Link
                            className="whitespace-nowrap underline underline-offset-4"
                            href={`/transaction-operations/runs/${encodeURIComponent(run.id)}`}
                          >
                            Review run<span className="sr-only"> {run.id}</span>
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
