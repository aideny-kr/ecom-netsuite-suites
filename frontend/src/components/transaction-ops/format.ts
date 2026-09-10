import type { JsonObject, RunScope } from "./types";

/** Exact server values stay strings; never parse monetary values as JS numbers. */
export function exactValue(value: unknown): string {
  if (value === null || value === undefined) return "Not provided";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
export function objectValue(value: unknown): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}
export function dateLabel(value: unknown): string {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value)))
    return "Not provided";
  return new Date(value)
    .toISOString()
    .replace("T", " ")
    .replace(/\.\d{3}Z$/, " UTC");
}
export function parseRunScope(
  mode: "references" | "window",
  references: string,
  start: string,
  end: string,
): RunScope {
  if (mode === "references") {
    const refs = Array.from(
      new Set(references.split(/[\s,]+/).filter(Boolean)),
    ).sort();
    if (!refs.length)
      throw new Error("Enter at least one full order reference.");
    if (refs.length > 200)
      throw new Error(
        "Investigate at most 200 full order references at a time.",
      );
    if (
      refs.some(
        (ref) => ref.length > 100 || !/^R\d{9}(?:-[A-Z0-9]+)?$/.test(ref),
      )
    )
      throw new Error(
        "Use full order references, including their suffix, such as R123456789-EU.",
      );
    return { order_references: refs };
  }
  if (
    !start ||
    !end ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(start) ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(end)
  )
    throw new Error("Enter both UTC window dates and times.");
  const first = Date.parse(`${start}:00Z`),
    last = Date.parse(`${end}:00Z`);
  if (!Number.isFinite(first) || !Number.isFinite(last) || last <= first)
    throw new Error("The window end must be after its start.");
  if (
    new Date(first).toISOString().slice(0, 16) !== start ||
    new Date(last).toISOString().slice(0, 16) !== end
  )
    throw new Error("Enter valid UTC calendar dates.");
  if (last - first > 31 * 86400_000)
    throw new Error("Use a window of at most 31 days.");
  return {
    window_start: new Date(first).toISOString(),
    window_end: new Date(last).toISOString(),
  };
}
export function runState(status: string, reason: string | null): string {
  if (status === "pending") return "Queued";
  if (status === "running") return "Investigating";
  return (
    (
      {
        done: "Investigation complete",
        budget: "Stopped at budget",
        stall: "Stopped for review",
        error: "Investigation error",
      } as Record<string, string>
    )[reason || ""] || "Status unavailable"
  );
}
export function proposalState(
  status: string,
  validUntil: string,
  operation: string | null | undefined,
  now: number,
): string {
  if (status === "approved") {
    return (
      (
        {
          verified: "Execution verified",
          executing: "Execution in progress",
          unknown: "Execution outcome unknown",
          failed: "Execution failed",
        } as Record<string, string>
      )[operation || ""] || "Approved — execution not verified"
    );
  }
  if (status === "pending")
    return Date.parse(validUntil) > now
      ? "Awaiting your review"
      : "Evidence expired";
  return (
    (
      { rejected: "Rejected", superseded: "Evidence superseded" } as Record<
        string,
        string
      >
    )[status] || "Status unavailable"
  );
}
export function actionLabel(action: unknown): string {
  return (
    (
      {
        sync_missing_order: "Sync missing order",
        correct_amounts: "Correct transaction amounts",
        resolve_celigo_error: "Resolve Celigo error",
        gather_evidence: "More evidence needed",
        human_review: "Review required",
        propose_missing_sync: "Missing order candidate",
        propose_amount_correction: "Amount correction candidate",
        propose_false_alarm_resolution: "False alarm candidate",
        no_action: "No action recommended",
      } as Record<string, string>
    )[String(action)] || "Review required"
  );
}
export function safeError(error: unknown): string {
  const status = objectValue(error).status;
  if (status === 403)
    return "Your access has changed. Refresh the page or contact an administrator.";
  if (status === 404)
    return "This record is unavailable in the current workspace.";
  if (status === 409)
    return "The state or evidence changed. Refresh status and start a new investigation if the evidence expired.";
  return "The request could not be confirmed. Refresh status before trying again.";
}
