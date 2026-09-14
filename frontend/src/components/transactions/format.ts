/** Format decimal strings without converting financial evidence to binary floats. */
export function transactionAmount(value: unknown, currency: unknown): string {
  if (typeof value !== "string" || !/^-?\d+(?:\.\d+)?$/.test(value)) return "—";
  let precision = 2;
  if (typeof currency === "string" && /^[A-Z]{3}$/.test(currency)) {
    precision = new Intl.NumberFormat("en-US", { style: "currency", currency }).resolvedOptions().minimumFractionDigits ?? 2;
  }
  const negative = value.startsWith("-");
  const [whole, fraction = ""] = value.replace(/^-/, "").split(".");
  const tail = fraction.replace(/0+$/, "").padEnd(precision, "0");
  const grouped = new Intl.NumberFormat("en-US").format(BigInt(whole));
  const amount = `${grouped}${tail ? `.${tail}` : ""}`;
  return negative && /[1-9]/.test(value) ? `(${amount})` : amount;
}

export function transactionDate(value: unknown): string {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) return "—";
  return new Intl.DateTimeFormat("en-US", {
    month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
  }).format(new Date(value));
}
