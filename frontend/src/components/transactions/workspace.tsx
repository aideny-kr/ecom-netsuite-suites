"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { SearchCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  useTransactionAccess,
  useTransactionConfigs,
} from "@/hooks/use-transaction-ops";
import { TransactionAccessBoundary } from "../transaction-ops/access-boundary";
import { ComparisonEvidence } from "../transaction-ops/evidence";
import {
  dateLabel,
  exactValue,
  objectValue,
  runState,
  safeError,
} from "../transaction-ops/format";
import type { TransactionRun } from "../transaction-ops/types";
import { BulkProposals } from "./bulk-proposals";
import { OrdersPage } from "./orders-page";
import {
  useReviewRuns,
  usePeriodData,
  useCases,
  useCaseEvidence,
  useFixProposals,
  useStartPeriodReview,
  useBulkCaseInvestigation,
  type PeriodInput,
  type ReviewRow,
} from "./review-hooks";
const input = "h-10 rounded-md border bg-background px-3 text-[13px]";
const verdicts: Record<string, string> = {
  matched: "Matched",
  difference: "Needs review",
  mismatch: "Needs review",
  missing_in_netsuite: "Missing in NetSuite",
  ambiguous: "Multiple matches",
  currency_mismatch: "Currency differs",
};
const runLink = (id: string) =>
  `/transaction-operations/runs/${encodeURIComponent(id)}`;
function span(run: TransactionRun) {
  return objectValue(run.params_json.review);
}
export function TransactionWorkspace() {
  const access = useTransactionAccess();
  return (
    <TransactionAccessBoundary>
      <Workspace key={access.tenantId} />
    </TransactionAccessBoundary>
  );
}
function Workspace() {
  const access = useTransactionAccess();
  const configs = useTransactionConfigs();
  const runs = useReviewRuns();
  const start = useStartPeriodReview();
  const investigate = useBulkCaseInvestigation();
  const [entity, setEntity] = useState("");
  const [period, setPeriod] = useState<PeriodInput["period"]>("last_week");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [pinned, setPinned] = useState<TransactionRun[]>([]);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("Orders");
  const [offset, setOffset] = useState(0);
  const [caseOffset, setCaseOffset] = useState(0);
  const [proposalOffset, setProposalOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [selectedCases, setSelectedCases] = useState<string[]>([]);
  const [caseId, setCaseId] = useState("");
  const [browse, setBrowse] = useState(false);
  const completed = useRef(new Map<string, TransactionRun>());
  const keys = useRef(new Map<string, string>());
  const batchKey = useRef<{ selection: string; key: string }>();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const scopes = (configs.data || []).filter((c) => !entity || c.id === entity);
  const startScopes = scopes.filter((c) => c.enabled);
  const reviewRuns = useMemo(
    () => (runs.data || []).filter((r) => typeof span(r).id === "string"),
    [runs.data],
  );
  const candidates = [...pinned, ...reviewRuns];
  const pinnedSelection = pinned.filter(
    (r) => !entity || r.config_id === entity,
  );
  const anchor =
    pinnedSelection[0] ||
    candidates.find((r) => scopes.some((c) => c.id === r.config_id));
  const selectedRuns = pinnedSelection.length
    ? pinnedSelection
    : scopes.flatMap((c) => {
        const found = candidates.find(
          (r) =>
            r.config_id === c.id &&
            span(r).start === (anchor && span(anchor).start) &&
            span(r).end === (anchor && span(anchor).end),
        );
        return found ? [found] : [];
      });
  const data = usePeriodData(
    selectedRuns.map((r) => r.id),
    offset,
    status,
    search.trim(),
  );
  const cases = useCases(caseOffset, tab === "Cases");
  const proposals = useFixProposals(proposalOffset, tab === "Fix approvals");
  const usable =
    selectedRuns.length > 0 && data.results.every((q) => q.data && !q.error);
  const totals = usable
    ? data.results.reduce(
        (sum, q) => {
          for (const key of Object.keys(sum) as (keyof typeof sum)[])
            sum[key] += q.data!.summary[key];
          return sum;
        },
        { checked: 0, matched: 0, needs_review: 0, not_verified: 0 },
      )
    : null;
  const rows = data.results.flatMap((q, index) =>
    (q.error ? [] : q.data?.items || []).map((row) => ({
      ...row,
      configId: selectedRuns[index].config_id,
    })),
  );
  const failure =
    error ||
    configs.error ||
    runs.error ||
    data.results.find((q) => q.error)?.error ||
    data.coverage.find((q) => q.error)?.error;
  async function reconcile() {
    if (!access.allowed || starting || !startScopes.length) return;
    setStarting(true);
    setError("");
    const queued: TransactionRun[] = [];
    const errors: string[] = [];
    for (const config of startScopes) {
      if (!alive.current) break;
      const body = {
        period,
        ...(period === "custom" ? { start_date: from, end_date: to } : {}),
      };
      const identity = JSON.stringify([access.tenantId, config.id, body]);
      const previous = completed.current.get(identity);
      if (previous) {
        queued.push(previous);
        continue;
      }
      if (!keys.current.has(identity))
        keys.current.set(identity, crypto.randomUUID());
      try {
        const run = await start.mutateAsync({
          configId: config.id,
          request: { ...body, evaluation_key: keys.current.get(identity)! },
        });
        queued.push(run);
        completed.current.set(identity, run);
      } catch (err) {
        errors.push(`${config.name}: ${safeError(err)}`);
      }
    }
    if (!errors.length) {
      keys.current.clear();
      completed.current.clear();
    }
    if (alive.current) {
      if (queued.length) {
        setPinned(queued);
        setOffset(0);
      }
      setError(errors.join(" "));
      setStarting(false);
    }
  }
  async function bulk(ids = selectedCases) {
    if (!ids.length || investigate.isPending) return;
    const selection = [...ids].sort().join(",");
    if (batchKey.current?.selection !== selection)
      batchKey.current = { selection, key: crypto.randomUUID() };
    try {
      await investigate.mutateAsync({
        case_ids: ids,
        evaluation_key: batchKey.current.key,
      });
      if (alive.current) {
        setSelectedCases([]);
        batchKey.current = undefined;
      }
    } catch {
      /* Mutation error is displayed with its retry state. */
    }
  }
  if (browse)
    return (
      <div className="space-y-5">
        <Button variant="outline" onClick={() => setBrowse(false)}>
          Back to period review
        </Button>
        <OrdersPage />
      </div>
    );
  return (
    <div className="animate-fade-in space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Transactions
          </h1>
          <p className="mt-2 text-[15px] text-muted-foreground">
            Review a period, investigate differences, and approve verified
            fixes.
          </p>
        </div>
        <Button variant="outline" onClick={() => setBrowse(true)}>
          Browse source orders
        </Button>
      </header>
      <section
        className="rounded-xl border bg-card p-5 shadow-soft"
        aria-label="Period review"
      >
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex flex-wrap gap-3">
            <select
              aria-label="Review period"
              className={input}
              value={period}
              disabled={starting}
              onChange={(e) =>
                setPeriod(e.target.value as PeriodInput["period"])
              }
            >
              <option value="last_week">Last week</option>
              <option value="last_month">Last month</option>
              <option value="yesterday">Yesterday</option>
              <option value="custom">Custom period</option>
            </select>
            <select
              aria-label="Review entity"
              className={input}
              value={entity}
              disabled={starting}
              onChange={(e) => {
                setEntity(e.target.value);
                setPinned([]);
                setOffset(0);
              }}
            >
              <option value="">All entities</option>
              {entity && !configs.data?.some((c) => c.id === entity) && (
                <option value={entity}>Historical scope</option>
              )}
              {(configs.data || []).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            {period === "custom" && (
              <>
                <input
                  aria-label="Period start date"
                  type="date"
                  className={input}
                  value={from}
                  onChange={(e) => setFrom(e.target.value)}
                />
                <input
                  aria-label="Period end date inclusive"
                  type="date"
                  className={input}
                  value={to}
                  onChange={(e) => setTo(e.target.value)}
                />
              </>
            )}
          </div>
          <Button
            disabled={
              starting ||
              !startScopes.length ||
              (period === "custom" && (!from || !to || from > to))
            }
            onClick={reconcile}
          >
            <SearchCheck className="mr-2 h-4 w-4" />
            {starting ? "Queueing reviews…" : "Reconcile period"}
          </Button>
        </div>
        <p className="mt-4 text-[13px] text-muted-foreground">
          Orders completed in the selected period, compared with current
          NetSuite records. Refund activity also checks older orders. Calendar
          boundaries use the configured business timezone.
        </p>
        <p className="mt-2 text-[13px]">
          {scopes.length &&
          scopes.every((c) => c.schedule_enabled && c.interval_minutes === 1440)
            ? "Daily checks on"
            : "Daily schedule varies by entity"}{" "}
          · Replica freshness is unverified; a completed scan is not financial
          certification.
        </p>
        {anchor && (
          <p className="mt-2 text-[13px] text-muted-foreground">
            Viewing {dateLabel(span(anchor).start)} →{" "}
            {dateLabel(span(anchor).end)} (end exclusive). Results available for{" "}
            {selectedRuns.length} of{" "}
            {Math.max(scopes.length, selectedRuns.length)} selected entities.
          </p>
        )}
        {!anchor && !runs.isLoading && !runs.error && (
          <p className="mt-3 text-[13px]">
            No period review available. Reconcile a period to collect real
            results.
          </p>
        )}
        <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-[13px]">
          {data.coverage.map((q, i) => (
            <span key={selectedRuns[i].id}>
              {
                configs.data?.find((c) => c.id === selectedRuns[i].config_id)
                  ?.name
              }
              :{" "}
              {q.error
                ? "Coverage unavailable"
                : q.data
                  ? `${q.data.complete ? "Scan complete" : q.data.status === "running" ? "Scanning" : "Needs attention"} · ${q.data.completed_slices} daily slices complete`
                  : "Loading coverage…"}
            </span>
          ))}
        </div>
      </section>
      {failure && (
        <p
          role="alert"
          className="rounded-lg border border-destructive/30 p-4 text-[13px] text-destructive"
        >
          Review data could not be loaded or queued. {safeError(failure)}
        </p>
      )}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {(
          [
            [
              "checked",
              "Orders checked",
              "Unique orders in available review results",
            ],
            ["matched", "Matched", "Order total, tax and refunds agree"],
            [
              "needs_review",
              "Needs review",
              "A difference or identity needs attention",
            ],
            [
              "not_verified",
              "Not verified",
              "Required comparison evidence is incomplete",
            ],
          ] as const
        ).map(([key, label, note]) => (
          <div key={key} className="rounded-xl border bg-card p-5 shadow-soft">
            <p className="text-[13px] text-muted-foreground">{label}</p>
            <p
              data-testid={`stat-${key}`}
              className="my-2 text-3xl font-semibold tabular-nums"
            >
              {totals?.[key] ?? "—"}
            </p>
            <p className="text-xs text-muted-foreground">{note}</p>
          </div>
        ))}
      </div>
      <div
        role="tablist"
        aria-label="Transaction views"
        className="flex gap-6 overflow-x-auto border-b"
      >
        {["Orders", "Refunds", "Cases", "Run history", "Fix approvals"].map(
          (name) => (
            <button
              role="tab"
              aria-selected={tab === name}
              key={name}
              className={`whitespace-nowrap border-b-2 px-1 py-3 text-[13px] ${tab === name ? "border-primary font-semibold" : "border-transparent text-muted-foreground"}`}
              onClick={() => setTab(name)}
            >
              {name}
            </button>
          ),
        )}
      </div>
      {(tab === "Orders" || tab === "Refunds") && (
        <section className="space-y-4">
          <div className="flex flex-wrap justify-between gap-3">
            <input
              aria-label="Search order number"
              placeholder="Search order number"
              className={`${input} w-full sm:w-80`}
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setOffset(0);
              }}
            />
            <select
              aria-label="Result status"
              className={input}
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">All results</option>
              <option value="needs_review">Needs review</option>
              <option value="matched">Matched</option>
              <option value="not_verified">Not verified</option>
            </select>
          </div>
          {tab === "Refunds" && (
            <p className="text-[13px] text-muted-foreground">
              Completed refund totals by order. Individual refund identity and
              integration delivery are not yet certified by these totals.
            </p>
          )}
          <div className="overflow-x-auto rounded-xl border">
            <table className="w-full text-[13px]">
              <thead className="bg-muted/30 text-left text-muted-foreground">
                <tr>
                  {[
                    "Order",
                    "Entity / currency",
                    ...(tab === "Refunds"
                      ? ["Solidus refunds", "NetSuite refunds", "Difference"]
                      : ["Order total", "Tax", "Refunds"]),
                    "Finding",
                    "Next step",
                  ].map((h) => (
                    <th className="whitespace-nowrap p-4 font-medium" key={h}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <ResultRow
                    key={`${row.configId}:${row.id}`}
                    row={row}
                    refunds={tab === "Refunds"}
                    name={
                      configs.data?.find((c) => c.id === row.configId)?.name ||
                      "Entity unavailable"
                    }
                    openCase={setCaseId}
                  />
                ))}
              </tbody>
            </table>
          </div>
          {!rows.length && (
            <p className="text-[13px] text-muted-foreground">
              {data.results.some((q) => q.isLoading)
                ? "Loading evidence…"
                : failure
                  ? "Results unavailable. Retry after the connection is restored."
                  : "No results in this view yet. This does not prove there are no exceptions."}
            </p>
          )}
          <Pagination
            offset={offset}
            size={25}
            hasNext={data.results.some((q) => q.data?.has_next)}
            setOffset={setOffset}
            label="25 rows per entity per page"
          />
        </section>
      )}
      {tab === "Cases" && (
        <section className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-[13px] text-muted-foreground">
              Open cases across all periods and entities. Select up to 50 for
              fresh investigation and supported proposals.
            </p>
            <Button
              disabled={!selectedCases.length || investigate.isPending}
              onClick={() => bulk()}
            >
              {investigate.isPending
                ? "Queueing…"
                : `Investigate selected (${selectedCases.length})`}
            </Button>
          </div>
          {(cases.error || investigate.error) && (
            <p role="alert" className="text-destructive">
              Cases could not be loaded or investigated.{" "}
              {safeError(cases.error || investigate.error)}
            </p>
          )}
          {investigate.data && (
            <div role="status" className="rounded-xl border p-4 text-[13px]">
              <p>
                Investigation results: {investigate.data.runs.length} scope
                batches queued. No financial change has been approved.
              </p>
              {investigate.data.runs.map((r) => (
                <Link
                  key={r.id}
                  className="mr-5 inline-block text-primary underline"
                  href={runLink(r.id)}
                >
                  Open investigation
                </Link>
              ))}
              {investigate.data.runs.length > 0 && (
                <Link
                  className="mt-3 block text-primary underline"
                  href={`/chat?${new URLSearchParams({ compose: `Review these batch investigations: ${investigate.data.runs.map((r) => runLink(r.id)).join(", ")}. Group supported fixes, identify blockers, and prepare exact proposals for my approval.`, new_session: "true" })}`}
                >
                  Work with agent on this batch
                </Link>
              )}
              {investigate.data.blocked.map((b) => (
                <p key={b.case_id}>
                  Case {b.case_id}: {b.code.replaceAll("_", " ")}
                </p>
              ))}
            </div>
          )}
          <div className="overflow-x-auto rounded-xl border">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b text-left text-muted-foreground">
                  {[
                    "Select",
                    "Order",
                    "Entity",
                    "Finding",
                    "Last observed",
                    "Next step",
                  ].map((h) => (
                    <th className="p-4 font-medium" key={h}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(cases.data || []).slice(0, 50).map((c) => (
                  <tr key={c.id} className="border-b last:border-0">
                    <td className="p-4">
                      <input
                        type="checkbox"
                        aria-label={`Select case ${c.order_reference}`}
                        checked={selectedCases.includes(c.id)}
                        disabled={
                          investigate.isPending ||
                          (!selectedCases.includes(c.id) &&
                            selectedCases.length >= 50)
                        }
                        onChange={(e) =>
                          setSelectedCases(
                            e.target.checked
                              ? [...selectedCases, c.id]
                              : selectedCases.filter((id) => id !== c.id),
                          )
                        }
                      />
                    </td>
                    <td className="p-4 font-medium">{c.order_reference}</td>
                    <td className="p-4">
                      {configs.data?.find(
                        (config) =>
                          config.subsidiary_id === c.scope_json.subsidiary_id,
                      )?.name ||
                        `Entity ${exactValue(c.scope_json.subsidiary_id)}`}
                    </td>
                    <td className="p-4">
                      {objectValue(c.latest_report_json.balance).status ===
                      "matched"
                        ? "Amounts agree · detail needs review"
                        : verdicts[
                            String(
                              objectValue(c.latest_report_json.balance).status,
                            )
                          ] || "Evidence needs review"}
                    </td>
                    <td className="whitespace-nowrap p-4 text-muted-foreground">
                      {dateLabel(c.last_observed_at)}
                    </td>
                    <td className="p-4">
                      <button
                        className="text-primary underline"
                        onClick={() => setCaseId(c.id)}
                      >
                        Review case
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!cases.data?.length && !cases.error && (
            <p className="text-[13px] text-muted-foreground">
              {cases.isLoading
                ? "Loading cases…"
                : "No open cases on this page."}
            </p>
          )}
          <Pagination
            offset={caseOffset}
            size={50}
            hasNext={(cases.data?.length || 0) > 50}
            setOffset={(n) => {
              setCaseOffset(n);
              setSelectedCases([]);
            }}
          />
        </section>
      )}
      {tab === "Run history" && (
        <section className="space-y-4">
          <p className="text-[13px] text-muted-foreground">
            Latest 200 investigations. A daily slice finishing does not mean the
            whole period has finished.
          </p>
          <div className="overflow-x-auto rounded-xl border">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b text-left text-muted-foreground">
                  {["Started", "Entity", "Progress", "Results"].map((h) => (
                    <th key={h} className="p-4 font-medium">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(runs.data || [])
                  .filter((r) => !entity || r.config_id === entity)
                  .map((r) => (
                    <tr key={r.id} className="border-b">
                      <td className="p-4">{dateLabel(r.created_at)}</td>
                      <td className="p-4">
                        {configs.data?.find((c) => c.id === r.config_id)
                          ?.name || "Historical scope"}
                      </td>
                      <td className="p-4">
                        {runState(r.status, r.termination_reason)}
                      </td>
                      <td className="space-x-4 p-4">
                        <Link
                          className="text-primary underline"
                          href={runLink(r.id)}
                        >
                          Open run
                        </Link>
                        {Boolean(span(r).id) && (
                          <button
                            className="text-primary underline"
                            onClick={() => {
                              setEntity(r.config_id);
                              setPinned([r]);
                              setOffset(0);
                              setTab("Orders");
                            }}
                          >
                            View period
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {tab === "Fix approvals" && (
        <section className="space-y-4">
          {proposals.error ? (
            <p role="alert">
              Fix proposals could not be loaded. {safeError(proposals.error)}
            </p>
          ) : proposals.isLoading ? (
            <p role="status">Loading fix proposals…</p>
          ) : (
            <BulkProposals
              key={proposalOffset}
              proposals={(proposals.data || []).slice(0, 20)}
            />
          )}
          <Pagination
            offset={proposalOffset}
            size={20}
            hasNext={(proposals.data?.length || 0) > 20}
            setOffset={setProposalOffset}
          />
        </section>
      )}
      <footer className="rounded-xl border bg-muted/20 p-4 text-[13px] text-muted-foreground">
        Daily detection, weekly or monthly review. Values come from recorded
        system evidence; different currencies are never combined into a monetary
        total. Fixes require exact human approval and independent execution
        verification.
      </footer>
      <CaseDrawer id={caseId} close={() => setCaseId("")} />
    </div>
  );
}
function ResultRow({
  row,
  refunds,
  name,
  openCase,
}: {
  row: ReviewRow;
  refunds: boolean;
  name: string;
  openCase: (id: string) => void;
}) {
  const balance = objectValue(row.balance);
  const amounts = objectValue(balance.amounts);
  const refund = objectValue(amounts.refunds);
  const values = refunds
    ? [refund.source, refund.target, refund.delta]
    : ["order_total", "tax", "refunds"].map(
        (k) => objectValue(amounts[k]).source,
      );
  return (
    <tr className="border-b last:border-0">
      <td className="whitespace-nowrap p-4 font-medium">
        {row.order_reference}
      </td>
      <td className="p-4">
        {name} · {exactValue(balance.currency)}
      </td>
      {values.map((v, i) => (
        <td
          key={i}
          className="whitespace-nowrap p-4 text-right font-mono tabular-nums"
        >
          {exactValue(v)}
        </td>
      ))}
      <td className="p-4">
        <span className="whitespace-nowrap rounded-full border px-2.5 py-1 text-xs">
          {verdicts[String(balance.status)] || "Not verified"}
        </span>
      </td>
      <td className="whitespace-nowrap p-4">
        {row.case_id ? (
          <button
            className="text-primary underline"
            onClick={() => openCase(row.case_id!)}
          >
            Review case →
          </button>
        ) : (
          <Link className="text-primary underline" href={runLink(row.run_id)}>
            View evidence →
          </Link>
        )}
      </td>
    </tr>
  );
}
function Pagination({
  offset,
  size,
  hasNext,
  setOffset,
  label,
}: {
  offset: number;
  size: number;
  hasNext: boolean;
  setOffset: (n: number) => void;
  label?: string;
}) {
  return (
    <div className="flex flex-wrap items-center justify-end gap-3 text-[13px]">
      <span className="text-muted-foreground">
        {label || `Page ${offset / size + 1}`}
      </span>
      <Button
        variant="outline"
        disabled={!offset}
        onClick={() => setOffset(Math.max(0, offset - size))}
      >
        Previous
      </Button>
      <Button
        variant="outline"
        disabled={!hasNext}
        onClick={() => setOffset(offset + size)}
      >
        Next
      </Button>
    </div>
  );
}
function CaseDrawer({ id, close }: { id: string; close: () => void }) {
  const { detail, history } = useCaseEvidence(id);
  const recheck = useBulkCaseInvestigation();
  const key = useRef<{ id: string; key: string }>();
  if (key.current?.id !== id) key.current = { id, key: crypto.randomUUID() };
  const c = detail.data;
  const compose = `Investigate transaction case ${id} using transaction_ops.status with case_id. Explain the evidence and prepare supported exact fixes for my approval. Do not execute an unapproved change.`;
  return (
    <Dialog
      open={!!id}
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <DialogContent className="fixed left-auto right-0 top-0 h-[100dvh] max-h-none w-full max-w-2xl translate-x-0 translate-y-0 overflow-y-auto rounded-none">
        <DialogHeader>
          <DialogTitle>
            {c ? c.order_reference : "Transaction case"}
          </DialogTitle>
          <DialogDescription>
            Review recorded evidence, investigate with the agent, and verify any
            approved outcome.
          </DialogDescription>
        </DialogHeader>
        {detail.error ? (
          <p role="alert">
            Case evidence could not be loaded. {safeError(detail.error)}
          </p>
        ) : !c ? (
          <p role="status">Loading evidence…</p>
        ) : (
          <div className="space-y-6">
            <p className="text-[13px] text-muted-foreground">
              {c.status === "reconciled" ? "Reconciled" : "Open case"} · Last
              observed {dateLabel(c.last_observed_at)}
            </p>
            <div className="flex flex-wrap gap-3">
              <Button asChild>
                <Link
                  href={`/chat?${new URLSearchParams({ compose, new_session: "true" })}`}
                >
                  Work with agent
                </Link>
              </Button>
              <Button
                variant="outline"
                disabled={recheck.isPending}
                onClick={() =>
                  recheck.mutate(
                    { case_ids: [id], evaluation_key: key.current!.key },
                    {
                      onSuccess: () => {
                        if (key.current?.id === id)
                          key.current = { id, key: crypto.randomUUID() };
                      },
                    },
                  )
                }
              >
                Recheck evidence
              </Button>
            </div>
            {recheck.error && <p role="alert">{safeError(recheck.error)}</p>}
            {recheck.data?.runs.map((r) => (
              <Link
                className="block text-primary underline"
                key={r.id}
                href={runLink(r.id)}
              >
                Open fresh investigation and proposals
              </Link>
            ))}
            <ComparisonEvidence report={c.latest_report_json} />
            <section className="space-y-3 border-t pt-5">
              <h3 className="font-semibold">Recorded history</h3>
              <p className="text-xs text-muted-foreground">
                Latest 20 observations. A new observation preserves the previous
                evidence.
              </p>
              {history.error && <p role="alert">History unavailable.</p>}
              {history.data?.map((h) => (
                <div
                  key={h.id}
                  className="flex justify-between gap-4 text-[13px]"
                >
                  <span>{dateLabel(h.observed_at)}</span>
                  <Link
                    className="text-primary underline"
                    href={runLink(h.run_id)}
                  >
                    View investigation
                  </Link>
                </div>
              ))}
            </section>
            <p className="rounded-lg border p-4 text-[13px] text-muted-foreground">
              A case does not authorize a change. Exact proposals appear under
              Fix approvals once required evidence and repair safeguards are
              verified.
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
