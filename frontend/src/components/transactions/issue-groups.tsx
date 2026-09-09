"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import {
  useTransactionAccess,
  useTransactionConfigs,
} from "@/hooks/use-transaction-ops";
import { Button } from "@/components/ui/button";
import { safeError } from "../transaction-ops/format";
import type { JsonObject, TransactionConfig } from "../transaction-ops/types";

type Group = {
  group_id: string;
  pattern: string;
  case_count: number;
  currency: string | null;
  scope: JsonObject;
  order_total: string;
  tax: string;
  refunds: string;
  target_state: string | null;
};
type Groups = { groups: Group[]; has_next: boolean };

function matches(config: TransactionConfig, scope: JsonObject) {
  return (
    [
      "source_connection_id",
      "source_step_id",
      "subsidiary_id",
      "record_type",
    ].every(
      (key) =>
        (config[key as keyof TransactionConfig] ?? null) ===
        (scope[key] ?? null),
    ) &&
    config.netsuite_account_id?.replaceAll("_", "-").toLowerCase() ===
      scope.netsuite_account_id
  );
}

export function IssueGroups() {
  const access = useTransactionAccess();
  const configs = useTransactionConfigs();
  const [offset, setOffset] = useState(0);
  const groups = useQuery({
    queryKey: ["transaction-ops", access.tenantId, "case-groups", offset],
    enabled: access.allowed,
    queryFn: () =>
      apiClient.get<Groups>(
        `/api/v1/transaction-ops/case-groups?limit=20&offset=${offset}`,
      ),
    refetchInterval: 30000,
  });
  return (
    <section
      className="space-y-4 rounded-xl border p-5"
      aria-label="Issue groups"
    >
      <div>
        <h2 className="font-semibold">Issue groups</h2>
        <p className="mt-1 text-[13px] text-muted-foreground">
          All open cases across periods and entities, grouped by issue pattern.
          The agent verifies a shared cause before proposing a batch fix.
        </p>
      </div>
      {groups.isLoading ? (
        <p className="text-sm">Loading issue groups…</p>
      ) : groups.error ? (
        <div className="flex items-center gap-3">
          <p role="alert" className="text-sm text-destructive">
            Issue groups unavailable. {safeError(groups.error)}
          </p>
          <Button variant="outline" onClick={() => groups.refetch()}>
            Retry groups
          </Button>
        </div>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[13px]">
              <thead>
                <tr className="border-b text-muted-foreground">
                  {["Pattern", "Entity / currency", "Cases", "Next step"].map(
                    (label) => (
                      <th key={label} className="p-3 font-medium">
                        {label}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {(groups.data?.groups || []).map((group) => {
                  const matching = (configs.data || []).filter((config) =>
                    matches(config, group.scope),
                  );
                  const active = matching.filter((config) => config.enabled);
                  const changes = [
                    ["order_total", "Order"],
                    ["tax", "Tax"],
                    ["refunds", "Refunds"],
                  ].flatMap(([key, label]) => {
                    const direction =
                      group[key as "tax" | "refunds" | "order_total"];
                    return direction === "positive" || direction === "negative"
                      ? [
                          `${label}: source ${direction === "positive" ? "higher" : "lower"}`,
                        ]
                      : [];
                  });
                  const prompt = `Investigate issue group ${group.group_id} (${group.pattern}). Use transaction_ops.groups with this group_id and follow every has_next page to include all current members. ${active.length === 1 ? `The exact active investigation config for this scope is ${active[0].id}.` : "Verify the exact configuration scope before starting any run."} Verify a shared cause across the cases; split any different causes. Use existing investigations or queue bounded fresh investigations for the exact order references in this scope. Prepare supported exact fixes together for human approval in Fix approvals. Do not approve or execute changes, issue duplicate refunds, or treat a group ID as authorization. Preserve per-case audit and independently verify each outcome.`;
                  return (
                    <tr key={group.group_id} className="border-b last:border-0">
                      <td className="p-3 font-medium">
                        {group.pattern}
                        <span className="mt-1 block text-xs font-normal text-muted-foreground">
                          {changes.join(" · ") || "Evidence requires review"}
                          {group.target_state ? ` · ${group.target_state}` : ""}
                        </span>
                      </td>
                      <td className="p-3">
                        {matching[0]?.name ||
                          `Entity ${String(group.scope.subsidiary_id ?? "unavailable")}`}
                        <span className="block text-xs text-muted-foreground">
                          {group.currency || "Currency unknown"}
                        </span>
                      </td>
                      <td className="p-3 font-mono tabular-nums">
                        {group.case_count}
                      </td>
                      <td className="p-3">
                        <Link
                          className="whitespace-nowrap text-primary underline"
                          href={`/chat?${new URLSearchParams({ compose: prompt, new_session: "true" })}`}
                        >
                          Investigate group →
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {!groups.data?.groups.length && (
            <p className="text-sm text-muted-foreground">
              No open issue groups found. Review run coverage for any unchecked
              orders.
            </p>
          )}
          <div className="flex items-center gap-3">
            <Button
              variant="outline"
              disabled={!offset}
              onClick={() => setOffset(Math.max(0, offset - 20))}
            >
              Previous groups
            </Button>
            <Button
              variant="outline"
              disabled={!groups.data?.has_next}
              onClick={() => setOffset(offset + 20)}
            >
              Next groups
            </Button>
          </div>
        </>
      )}
      <p className="text-xs text-muted-foreground">
        Investigate together → review exact fixes → approve the selected batch →
        execute and verify each order. Every change retains its own approval and
        audit trail.
      </p>
    </section>
  );
}
