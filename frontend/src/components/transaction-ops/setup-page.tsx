"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { useTransactionAccess } from "@/hooks/use-transaction-ops";
import {
  useCreateTransactionConfig,
  useTransactionSetupOptions,
  type StepOption,
} from "@/hooks/use-transaction-setup";
import { TransactionAccessBoundary } from "./access-boundary";
import { cardClass, inputClass } from "./evidence";
import { safeError } from "./format";
import {
  buildConfigInput,
  emptyDraft,
  type MappingRow,
  type ScopeDraft,
} from "./setup-input";

type Column = { key: string; label: string; options?: [string, string][] };
const rounding: [string, string][] = [
  ["half_up", "Half up"],
  ["half_even", "Half even"],
];
function MappingRows({
  title,
  columns,
  rows,
  change,
}: {
  title: string;
  columns: Column[];
  rows: MappingRow[];
  change: (rows: MappingRow[]) => void;
}) {
  return (
    <fieldset className="space-y-3">
      <legend className="mb-2 font-medium">{title}</legend>
      {rows.map((row, index) => (
        <div
          key={index}
          className="flex flex-wrap items-end gap-3 rounded-lg border p-3"
        >
          {columns.map((column) => (
            <label
              key={column.key}
              className="min-w-32 flex-1 space-y-1 text-[13px]"
            >
              {column.label}
              {column.options ? (
                <select
                  className={inputClass}
                  aria-label={`${title} ${index + 1} ${column.label}`}
                  value={row[column.key] || ""}
                  onChange={(e) =>
                    change(
                      rows.map((value, i) =>
                        i === index
                          ? { ...value, [column.key]: e.target.value }
                          : value,
                      ),
                    )
                  }
                >
                  <option value="">Choose…</option>
                  {column.options.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  className={inputClass}
                  aria-label={`${title} ${index + 1} ${column.label}`}
                  maxLength={255}
                  value={row[column.key] || ""}
                  onChange={(e) =>
                    change(
                      rows.map((value, i) =>
                        i === index
                          ? { ...value, [column.key]: e.target.value }
                          : value,
                      ),
                    )
                  }
                />
              )}
            </label>
          ))}
          <Button
            type="button"
            variant="ghost"
            aria-label={`Remove ${title} ${index + 1}`}
            onClick={() => change(rows.filter((_, i) => i !== index))}
          >
            Remove
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        onClick={() => change([...rows, {}])}
      >
        Add {title.toLowerCase()}
      </Button>
    </fieldset>
  );
}
function stepLabel(step: StepOption) {
  const environment =
    step.sandbox === true
      ? "Sandbox"
      : step.sandbox === false
        ? "Production"
        : "Environment unknown";
  return `${step.integration_name} / ${step.flow_name} / ${step.reference_name || "Unnamed step"} · ${environment}`;
}
export function TransactionSetupPage() {
  const access = useTransactionAccess();
  return (
    <TransactionAccessBoundary>
      {access.canManage ? (
        <SetupForm key={access.tenantId} />
      ) : (
        <p>
          Connection-management permission is required to configure
          investigation scopes.
        </p>
      )}
    </TransactionAccessBoundary>
  );
}
function SetupForm() {
  const options = useTransactionSetupOptions(),
    create = useCreateTransactionConfig();
  const [draft, setDraft] = useState(emptyDraft),
    [error, setError] = useState(""),
    [created, setCreated] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const pages = options.data?.pages || [],
    sources = pages.flatMap((page) => page.source_steps),
    targets = pages.flatMap((page) => page.target_steps),
    connections = pages.flatMap((page) => page.netsuite_connections);
  function update<K extends keyof ScopeDraft>(key: K, value: ScopeDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    let body;
    try {
      body = buildConfigInput(draft);
    } catch (err) {
      setError((err as Error).message);
      return;
    }
    try {
      const result = await create.mutateAsync(body);
      if (mounted.current) setCreated(result.name);
    } catch (err) {
      if (mounted.current) setError(safeError(err));
    }
  }
  const text = (
    label: string,
    key:
      | "name"
      | "account"
      | "subsidiary"
      | "reference"
      | "legacyTaxCode"
      | "interval"
      | "orders"
      | "calls"
      | "deadline",
    placeholder?: string,
  ) => (
    <label className="block space-y-2 text-[13px]">
      {label}
      <input
        className={inputClass}
        value={draft[key]}
        placeholder={placeholder}
        maxLength={255}
        onChange={(e) => update(key, e.target.value)}
      />
    </label>
  );
  if (created)
    return (
      <section className={`${cardClass} space-y-4`}>
        <h1 className="text-2xl font-semibold">Scope created</h1>
        <p>{created} is ready for investigations.</p>
        <p className="text-muted-foreground">
          Each run verifies the live source and destination. Missing evidence
          will remain visible for review.
        </p>
        <Button asChild>
          <Link href="/transaction-operations">Open investigations</Link>
        </Button>
      </section>
    );
  return (
    <div className="space-y-8 animate-fade-in text-[15px]">
      <header>
        <Link
          className="text-[13px] text-muted-foreground underline"
          href="/transaction-operations"
        >
          Transaction operations
        </Link>
        <h1 className="mt-4 text-2xl font-semibold">
          Connect an investigation scope
        </h1>
        <p className="mt-2 text-muted-foreground">
          Choose the source and destination explicitly. Mapping changes require
          a new scope so earlier evidence stays tied to its original rules.
        </p>
      </header>
      {options.isLoading ? (
        <p role="status">Loading connection candidates…</p>
      ) : options.error ? (
        <p role="alert">
          Connection candidates could not be loaded. {safeError(options.error)}
        </p>
      ) : null}
      <form onSubmit={submit}>
        <fieldset
          disabled={create.isPending || options.isLoading || !!options.error}
          className="space-y-8"
        >
          <section className={`${cardClass} space-y-5`}>
            <h2 className="text-lg font-semibold">Source &amp; destination</h2>
            {text("Scope name", "name")}
            <div className="grid gap-5 md:grid-cols-2">
              <label className="block space-y-2 text-[13px]">
                Framework source candidate
                <select
                  className={inputClass}
                  value={draft.sourceId}
                  onChange={(e) => update("sourceId", e.target.value)}
                >
                  <option value="">Choose a source…</option>
                  {sources.map((step) => (
                    <option key={step.id} value={step.id}>
                      {stepLabel(step)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block space-y-2 text-[13px]">
                NetSuite connection
                <select
                  className={inputClass}
                  value={draft.connectionId}
                  onChange={(e) => {
                    const selected = connections.find(
                      (item) => item.id === e.target.value,
                    );
                    setDraft((current) => ({
                      ...current,
                      connectionId: e.target.value,
                      account: selected?.account_id || "",
                    }));
                  }}
                >
                  <option value="">Choose a destination…</option>
                  {connections.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.label} ·{" "}
                      {item.account_id || "Account ID unconfirmed"}
                    </option>
                  ))}
                </select>
              </label>
              {text("NetSuite account ID", "account", "1234567_SB1")}
              {text("Destination subsidiary ID", "subsidiary")}
            </div>
            <label className="block space-y-2 text-[13px]">
              Celigo transaction step (optional)
              <select
                className={inputClass}
                value={draft.targetId}
                onChange={(e) => update("targetId", e.target.value)}
              >
                <option value="">Do not correlate a Celigo step</option>
                {targets.map((step) => (
                  <option key={step.id} value={step.id}>
                    {stepLabel(step)}
                  </option>
                ))}
              </select>
            </label>
            <p className="text-[13px] text-muted-foreground">
              These are mirrored candidates. Every investigation verifies the
              live Framework connection and exact NetSuite account. The source
              environment is shown beside each step.
            </p>
            {!sources.length || !connections.length ? (
              <p className="text-[13px]">
                Missing a connection?{" "}
                <Link className="underline" href="/connections">
                  Open Connections
                </Link>{" "}
                and refresh the Celigo flow mirror.
              </p>
            ) : null}
            {options.hasNextPage && (
              <Button
                type="button"
                variant="outline"
                disabled={options.isFetchingNextPage}
                onClick={() => options.fetchNextPage()}
              >
                Load more connection options
              </Button>
            )}
          </section>
          <section className={`${cardClass} space-y-6`}>
            <h2 className="text-lg font-semibold">Transaction mappings</h2>
            {text("Full order reference field", "reference", "tranid")}
            <label className="block space-y-2 text-[13px]">
              Match order lines by
              <select
                className={inputClass}
                value={draft.lineIdentity}
                onChange={(e) => update("lineIdentity", e.target.value)}
              >
                <option value="source_line_id">Source line ID</option>
                <option value="inventory_units">
                  Inventory IDs and original SKU
                </option>
              </select>
            </label>
            <p className="text-[13px] text-muted-foreground">
              {draft.lineIdentity === "inventory_units"
                ? "Each native line must contain the complete source inventory set and matching original SKU. Missing or shared IDs keep the finding open."
                : "Use this policy only when the import stores the source line ID on every NetSuite line."}
            </p>
            <label className="block space-y-2 text-[13px]">
              Native tax layout
              <select
                className={inputClass}
                value={draft.legacyTaxMode}
                onChange={(e) => {
                  update("legacyTaxMode", e.target.value);
                  if (!e.target.value) update("legacyTaxCode", "");
                }}
              >
                <option value="">SuiteTax or unconfigured</option>
                <option value="aggregate_header">
                  Legacy aggregate header tax
                </option>
                <option value="line_tax_amount">
                  Legacy tax amounts on each line
                </option>
              </select>
            </label>
            {draft.legacyTaxMode && (
              <>
                {text("Native tax code ID", "legacyTaxCode")}
                <p className="text-[13px] text-muted-foreground">
                  This tax profile uses the destination account and subsidiary
                  selected above. Source tax rules must still validate each
                  adjustment.
                  {draft.legacyTaxMode === "aggregate_header" &&
                    " Header corrections also require explicit NetSuite rounding and zero shipping."}
                </p>
              </>
            )}
            <p className="text-[13px] text-muted-foreground">
              Enter verified mappings only. Blank metadata stays unknown;
              currency never selects a subsidiary. Use “legacy” only for source
              orders whose business entity is explicitly absent.
            </p>
            <MappingRows
              title="Currency"
              columns={[
                { key: "code", label: "ISO code" },
                { key: "places", label: "Decimal places" },
              ]}
              rows={draft.currencies}
              change={(rows) => update("currencies", rows)}
            />
            <MappingRows
              title="Business entity"
              columns={[
                { key: "entity", label: "Source entity" },
                { key: "subsidiary", label: "NetSuite subsidiary ID" },
              ]}
              rows={draft.entities}
              change={(rows) => update("entities", rows)}
            />
            <label className="block space-y-2 text-[13px]">
              Source tax evidence
              <select
                className={inputClass}
                value={draft.taxEvidence}
                onChange={(e) => {
                  update("taxEvidence", e.target.value);
                  update(
                    "taxes",
                    draft.taxes.map(
                      ({ rate: _rate, rounding: _rounding, ...row }) => row,
                    ),
                  );
                }}
              >
                <option value="statutory_rate">Statutory rate (default)</option>
                <option value="source_assessment">
                  Finalized Framework assessment
                </option>
              </select>
            </label>
            {draft.taxEvidence === "source_assessment" && (
              <p className="rounded-md border bg-muted/40 p-4 text-[13px]">
                Each tax rule uses final Framework adjustment amounts, IDs and
                timestamps. Statutory rates are not independently verified. This
                limitation remains visible in every approval.
              </p>
            )}
            <MappingRows
              title="Tax rule"
              columns={[
                { key: "source", label: "Source tax rate ID" },
                ...(draft.taxEvidence === "statutory_rate"
                  ? [{ key: "rate", label: "Rate fraction" }]
                  : []),
                {
                  key: "basis",
                  label: "Tax basis",
                  options: [
                    ["included", "Included"],
                    ["additional", "Additional"],
                  ],
                },
                ...(draft.taxEvidence === "statutory_rate"
                  ? [{ key: "rounding", label: "Rounding", options: rounding }]
                  : []),
                { key: "destination", label: "NetSuite tax ID" },
              ]}
              rows={draft.taxes}
              change={(rows) => update("taxes", rows)}
            />
            <p className="text-[13px] text-muted-foreground">
              {draft.taxEvidence === "statutory_rate"
                ? "Rates are fractions: enter 0.20 for 20%. Source tax IDs come from the order’s adjustments; a label or effective aggregate rate is insufficient."
                : "Enter each exact source tax ID and whether the amount is included or additional. Its native tax ID must match the legacy tax profile above."}
            </p>
            <label className="block space-y-2 text-[13px]">
              NetSuite tax component rounding
              <select
                className={inputClass}
                value={draft.taxRounding}
                onChange={(e) => update("taxRounding", e.target.value)}
              >
                <option value="">Unknown</option>
                {rounding.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          </section>
          <section className={`${cardClass} space-y-5`}>
            <h2 className="text-lg font-semibold">Run controls</h2>
            <label className="flex items-center gap-3">
              <input
                type="checkbox"
                checked={draft.propose}
                onChange={(e) => update("propose", e.target.checked)}
              />
              Prepare actions for human review
            </label>
            <p className="text-[13px] text-muted-foreground">
              Off: collect findings only. On: complete evidence under your
              verified mapping may produce proposals. Every external write still
              needs separate human approval.
            </p>
            <label className="flex items-center gap-3">
              <input
                type="checkbox"
                checked={draft.schedule}
                onChange={(e) => update("schedule", e.target.checked)}
              />
              Enable scheduled investigations
            </label>
            <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
              {text("Interval (minutes)", "interval")}
              {text("Order limit", "orders")}
              {text("API call limit", "calls")}
              {text("Deadline (seconds)", "deadline")}
            </div>
            <p className="text-[13px] text-muted-foreground">
              Schedules begin with the last interval and retain unfinished scan
              progress when a run reaches its budget.
            </p>
          </section>
          {error && (
            <p className="rounded-lg border p-4" role="alert">
              {error}
            </p>
          )}
          <Button type="submit">
            {create.isPending ? "Saving scope…" : "Create investigation scope"}
          </Button>
        </fieldset>
      </form>
    </div>
  );
}
