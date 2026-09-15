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
  const amounts = objectValue(objectValue(balance).amounts);
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
