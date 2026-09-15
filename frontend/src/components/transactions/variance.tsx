import { objectValue } from "../transaction-ops/format";

/** Display the server's exact delta; never round money through a JS number. */
export function deltaValue(value: unknown) {
  if (
    typeof value !== "string" ||
    !/^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(value)
  )
    return { text: "—", nonzero: false };
  const zero = /^[+-]?0+(?:\.0+)?(?:[eE][+-]?\d+)?$/.test(value);
  return {
    text: zero
      ? "0.00"
      : value.startsWith("-") || value.startsWith("+")
        ? value
        : `+${value}`,
    nonzero: !zero,
  };
}

export function Variance({ balance }: { balance: unknown }) {
  const parsed = objectValue(balance);
  const posting = objectValue(parsed.posting_reconciliation);
  if (posting.basis === "verified_source_revision_and_owned_credit_refund") {
    return <PostingBalanceComparison posting={posting} />;
  }
  const amounts = objectValue(parsed.amounts);
  return (
    <dl
      className="grid min-w-36 grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs"
      aria-label="Variance: source minus ERP"
    >
      {[
        ["order_total", "Order"],
        ["tax", "VAT / tax"],
        ["refunds", "Refunds"],
      ].map(([key, label]) => {
        const value = deltaValue(objectValue(amounts[key]).delta);
        return (
          <div
            key={key}
            className={`contents ${value.nonzero ? "text-orange-700 dark:text-orange-300" : "text-muted-foreground"}`}
          >
            <dt>{label}</dt>
            <dd className="text-right font-mono tabular-nums">{value.text}</dd>
          </div>
        );
      })}
    </dl>
  );
}

export function PostingBalanceComparison({ posting }: { posting: Record<string, unknown> }) {
  const amounts = objectValue(posting.amounts);
  const alignment = objectValue(posting.sales_order_alignment);
  const orderAmounts = objectValue(alignment.amounts);
  return <div className="min-w-80 space-y-3 text-xs">
    <table className="w-full tabular-nums" aria-label="Posting comparison: source and invoice less credit">
      <thead><tr className="text-muted-foreground">
        <th className="pb-2 text-left font-medium">Posting</th>
        <th className="pb-2 text-right font-medium">Source</th>
        <th className="pb-2 pl-3 text-right font-medium">Invoice − credit</th>
        <th className="pb-2 pl-3 text-right font-medium">Variance</th>
      </tr></thead>
      <tbody>{[["net", "Before tax"], ["tax", "VAT / tax"], ["order_total", "Total"], ["refunds", "Refunds"]].map(([key, label]) => {
        const metric = objectValue(amounts[key]);
        const delta = deltaValue(metric.delta);
        return <tr key={key}>
          <th className="py-1 text-left font-normal">{label}</th>
          <td className="text-right font-mono">{typeof metric.source === "string" ? metric.source : "—"}</td>
          <td className="pl-3 text-right font-mono">{typeof metric.target === "string" ? metric.target : "—"}</td>
          <td className={`pl-3 text-right font-mono ${delta.nonzero ? "text-orange-700 dark:text-orange-300" : "text-muted-foreground"}`}>{delta.text}</td>
        </tr>;
      })}</tbody>
    </table>
    <div className="border-t pt-2 text-muted-foreground">
      <p className="font-medium">Sales order · non-posting{alignment.status === "matched" ? " · amounts agree" : " · original order comparison"}</p>
      {alignment.status !== "matched" && <p className="mt-1">Current source − original order <span className="font-mono">{deltaValue(objectValue(orderAmounts.order_total).delta).text}</span>
        {" · Tax "}<span className="font-mono">{deltaValue(objectValue(orderAmounts.tax).delta).text}</span></p>}
      {alignment.status !== "matched" && <p className="mt-1">A later credit can explain this difference. Amend the original order only if separate evidence shows it was incorrect.</p>}
      {typeof posting.observed_at === "string" && <p className="mt-1">Accounting evidence: {posting.observed_at.replace("T", " ").replace(/\.\d+(?:\+00:00|Z)$/, " UTC")}</p>}
    </div>
  </div>;
}
