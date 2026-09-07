import { actionLabel, dateLabel, exactValue, objectValue } from "./format";
import type { JsonObject, TransactionFinding } from "./types";

export const cardClass = "rounded-xl border bg-card p-5 shadow-soft";
export const inputClass =
  "w-full rounded-md border border-input bg-background px-3 py-2 text-[15px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
export function Status({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex rounded-full border bg-muted/50 px-3 py-1 text-[13px] font-medium">
      {children}
    </span>
  );
}
export function ExactChanges({
  before,
  after,
}: {
  before: JsonObject;
  after: JsonObject;
}) {
  const keys = Array.from(
    new Set([...Object.keys(before), ...Object.keys(after)]),
  ).sort();
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[13px]">
        <caption className="sr-only">Immutable proposed changes</caption>
        <thead>
          <tr className="border-b text-left text-muted-foreground">
            <th className="py-3 pr-4 font-medium">Field</th>
            <th className="p-3 font-medium">Exact before</th>
            <th className="p-3 font-medium">Exact after</th>
          </tr>
        </thead>
        <tbody>
          {keys.map((key) => (
            <tr key={key} className="border-b last:border-0">
              <th
                scope="row"
                className="py-3 pr-4 text-left align-top font-medium break-words"
              >
                {key}
              </th>
              <td className="p-3 align-top">
                <pre className="max-w-lg whitespace-pre-wrap break-all font-mono tabular-nums">
                  {exactValue(before[key])}
                </pre>
              </td>
              <td className="p-3 align-top">
                <pre className="max-w-lg whitespace-pre-wrap break-all font-mono tabular-nums">
                  {exactValue(after[key])}
                </pre>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!keys.length && (
        <p className="py-4 text-[13px] text-muted-foreground">
          No amount fields supplied. Review the exact action and evidence.
        </p>
      )}
    </div>
  );
}
export function EvidenceJson({
  value,
  label = "Full recorded evidence",
}: {
  value: unknown;
  label?: string;
}) {
  return (
    <details className="rounded-lg border bg-muted/20 p-3 text-[13px]">
      <summary className="cursor-pointer font-medium">{label}</summary>
      <pre className="mt-3 max-h-[32rem] overflow-auto whitespace-pre-wrap break-all text-xs leading-relaxed">
        {exactValue(value)}
      </pre>
    </details>
  );
}
export function TaxEvidenceNotice({ evidence }: { evidence: JsonObject }) {
  const report = Object.keys(objectValue(evidence.report)).length
    ? objectValue(evidence.report)
    : evidence;
  const source = objectValue(report.source);
  if (
    !Array.isArray(source.tax_details) ||
    !source.tax_details.some(
      (tax) => objectValue(tax).calculation === "source_assessment",
    )
  )
    return null;
  return (
    <div className="rounded-md border bg-muted/40 p-4 text-[13px]">
      <p className="font-semibold">Source tax evidence</p>
      <p className="mt-2">
        Tax policy: finalized Framework assessments. Statutory rates are not
        independently verified.
      </p>
    </div>
  );
}
export function ComparisonEvidence({ report }: { report: JsonObject }) {
  const comparison = objectValue(report.comparison);
  const reasons = Array.isArray(comparison.findings)
    ? comparison.findings.map(objectValue)
    : [];
  const differences = Array.isArray(comparison.differences)
    ? comparison.differences.map(objectValue)
    : [];
  const currency =
    typeof comparison.currency === "string" ? comparison.currency : null;
  return (
    <div className="space-y-4">
      <BalanceEvidence balance={objectValue(report.balance)} />
      <TaxEvidenceNotice evidence={report} />
      {reasons.length > 0 && (
        <ul className="space-y-2 text-[13px]">
          {reasons.map((item, i) => (
            <li key={i}>
              <span className="font-medium">
                {String(item.code || "Finding").replaceAll("_", " ")}
              </span>
              {typeof item.reason === "string" && (
                <span className="text-muted-foreground"> — {item.reason}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {differences.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-[13px] tabular-nums">
            <caption className="py-2 text-left font-medium">
              Transaction amounts · {currency || "Currency not established"}
            </caption>
            <thead>
              <tr className="border-b text-right text-muted-foreground">
                <th className="py-3 pr-4 text-left font-medium">Field</th>
                <th className="p-3 font-medium">Framework</th>
                <th className="p-3 font-medium">NetSuite</th>
                <th className="p-3 font-medium">
                  Difference
                  <br />
                  <span className="text-xs">source − target</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {differences.map((item, i) => (
                <tr key={i} className="border-b last:border-0">
                  <th
                    scope="row"
                    className="py-3 pr-4 text-left font-medium break-words"
                  >
                    {String(item.field || "Amount")}
                  </th>
                  {[item.source, item.target, item.delta].map((amount, j) => (
                    <td
                      key={j}
                      className="p-3 text-right font-mono whitespace-nowrap"
                    >
                      {typeof amount === "string" ? amount : "Not provided"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-[13px] text-muted-foreground">
        Amounts and differences are server-computed exact strings in transaction
        currency. Currencies are never combined. Unknown evidence is not treated
        as zero.
      </p>
    </div>
  );
}
function BalanceEvidence({ balance }: { balance: JsonObject }) {
  const amounts = objectValue(balance.amounts);
  if (!Object.keys(amounts).length) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[13px] tabular-nums">
        <caption className="py-2 text-left font-medium">
          Order reconciliation ·{" "}
          {String(balance.currency || "Currency unknown")}
        </caption>
        <thead>
          <tr className="border-b text-right text-muted-foreground">
            <th className="py-3 pr-4 text-left font-medium">Amount</th>
            <th className="p-3 font-medium">Solidus</th>
            <th className="p-3 font-medium">
              NetSuite
              {balance.target_currency &&
              balance.target_currency !== balance.currency
                ? ` · ${balance.target_currency}`
                : ""}
            </th>
            <th className="p-3 font-medium">Difference (source − target)</th>
          </tr>
        </thead>
        <tbody>
          {[
            ["order_total", "Order total"],
            ["tax", "VAT / tax"],
            ["refunds", "Completed refunds"],
          ].map(([key, label]) => {
            const metric = objectValue(amounts[key]);
            return (
              <tr key={key} className="border-b last:border-0">
                <th scope="row" className="py-3 pr-4 text-left font-medium">
                  {label}
                </th>
                {["source", "target", "delta"].map((side) => (
                  <td
                    key={side}
                    className="p-3 text-right font-mono whitespace-nowrap"
                  >
                    {typeof metric[side] === "string"
                      ? (metric[side] as string)
                      : "Unknown"}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-2 text-[13px] text-muted-foreground">
        Gross includes tax. Completed refunds are compared separately. Repair
        eligibility also requires the supporting record details.
      </p>
    </div>
  );
}
export function FindingCard({ finding }: { finding: TransactionFinding }) {
  const comparison = objectValue(finding.report_json.comparison);
  return (
    <details className={cardClass}>
      <summary className="flex cursor-pointer flex-wrap items-center justify-between gap-3">
        <span className="font-semibold break-all">
          {finding.order_reference}
        </span>
        <span className="flex flex-wrap items-center gap-3">
          <span className="text-[13px] text-muted-foreground">
            {typeof comparison.currency === "string"
              ? comparison.currency
              : "Currency not established"}
          </span>
          <Status>{actionLabel(comparison.recommended_action)}</Status>
        </span>
      </summary>
      <div className="mt-5 space-y-4">
        <ComparisonEvidence report={finding.report_json} />
        <p className="text-[13px] text-muted-foreground">
          Recorded {dateLabel(finding.updated_at)}
        </p>
        <EvidenceJson value={finding.report_json} />
      </div>
    </details>
  );
}
