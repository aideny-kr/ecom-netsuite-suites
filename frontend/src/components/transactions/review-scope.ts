import type { TransactionConfig, TransactionRun } from "../transaction-ops/types";

export function configForRun(
  configs: TransactionConfig[],
  run: Pick<TransactionRun, "config_id" | "tenant_id" | "config_snapshot">,
) {
  const exact = configs.find(
    (config) => config.id === run.config_id &&
      (!run.tenant_id || config.tenant_id === run.tenant_id),
  );
  if (exact) return exact;
  const snapshot = run.config_snapshot || {};
  const account = (value: unknown) =>
    typeof value === "string" ? value.toLowerCase().replaceAll("_", "-") : "";
  const candidates = configs.filter((config) => {
    if (
      !run.tenant_id || config.tenant_id !== run.tenant_id ||
      !(config.source_connection_id || config.source_step_id) ||
      !config.netsuite_account_id || !config.subsidiary_id || !config.record_type
    ) return false;
    return (
      account(config.netsuite_account_id) === account(snapshot.netsuite_account_id) &&
      config.subsidiary_id === snapshot.subsidiary_id &&
      config.record_type === snapshot.record_type &&
      (config.source_connection_id ?? null) === (snapshot.source_connection_id ?? null) &&
      (config.source_step_id ?? null) === (snapshot.source_step_id ?? null)
    );
  });
  // Two independently configured scopes must not double-count one review.
  return candidates.length === 1 ? candidates[0] : undefined;
}
