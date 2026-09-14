import { expect, it } from "vitest";
import type { TransactionConfig, TransactionRun } from "../transaction-ops/types";
import { configForRun } from "./review-scope";

const scope = {
  source_connection_id: "source-a",
  source_step_id: null,
  netsuite_account_id: "account_SB1",
  subsidiary_id: "2",
  record_type: "salesOrder",
};
const config = { id: "new", tenant_id: "tenant-a", ...scope } as TransactionConfig;
const run: Pick<TransactionRun, "config_id" | "tenant_id" | "config_snapshot"> = {
  config_id: "old", tenant_id: "tenant-a", config_snapshot: scope,
};

it("retains an entity across configuration revisions without changing the run identity", () => {
  expect(configForRun([config], run)).toBe(config);
  expect(run.config_id).toBe("old");
});
it.each(Object.keys(scope))("does not combine a different %s", (key) => {
  expect(configForRun([config], {
    ...run, config_snapshot: { ...scope, [key]: "different" },
  })).toBeUndefined();
});
it("rejects cross-tenant, incomplete and ambiguous scopes", () => {
  expect(configForRun([config], { ...run, tenant_id: "tenant-b" })).toBeUndefined();
  expect(configForRun([config], { ...run, config_snapshot: {} })).toBeUndefined();
  expect(configForRun([config, { ...config, id: "another" }], run)).toBeUndefined();
});
it("uses an exact current config before considering historical scope equivalence", () => {
  const current = { ...config, id: run.config_id };
  expect(configForRun([config, current], run)).toBe(current);
});
