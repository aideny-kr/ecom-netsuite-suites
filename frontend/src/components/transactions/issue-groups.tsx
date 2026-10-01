"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import {
  useTransactionAccess,
  useTransactionConfigs,
} from "@/hooks/use-transaction-ops";
import { Pagination } from "./pagination";
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
type Groups = {
  groups: Group[];
  has_next: boolean;
  total_groups: number;
  total_cases: number;
};

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

export function IssueGroups({
  reviewRunIds,
  status = "needs_review",
  search = "",
}: {
  reviewRunIds?: string[];
  status?: string;
  search?: string;
}) {
  const access = useTransactionAccess();
  const configs = useTransactionConfigs();
  const [offset, setOffset] = useState(0);
  const [size, setSize] = useState(50);
  const scope = reviewRunIds
    ? { review_run_ids: reviewRunIds, status, search }
    : {};
  const query = new URLSearchParams({
    limit: String(size),
    offset: String(offset),
  });
  if (reviewRunIds) {
    reviewRunIds.forEach((id) => query.append("review_run_ids", id));
    query.set("status", status);
    if (search) query.set("search", search);
  }
  const groups = useQuery({
    queryKey: [
      "transaction-ops",
      access.tenantId,
      "case-groups",
      scope,
      offset,
      size,
    ],
    enabled:
      access.allowed && (reviewRunIds === undefined || reviewRunIds.length > 0),
    queryFn: () =>
      apiClient.get<Groups>(`/api/v1/transaction-ops/case-groups?${query}`),
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
          {reviewRunIds
            ? `${status === "not_verified" ? "Not verified" : "Needs review"} orders in the selected period and entities${search ? ", matching your search" : ""}, grouped by issue pattern.`
            : "All open cases across periods and entities, including historical cases. These counts differ from the selected period."}{" "}
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
                  const exactScope = reviewRunIds ? ` and these exact scope parameters: ${JSON.stringify(scope)}` : "";
                  // A group shares a symptom, not a cause: break it down before preparing anything.
                  const explain = `Explain issue group ${group.group_id} (${group.pattern}). Call transaction_ops.group_breakdown with group_id "${group.group_id}"${exactScope}. Explain each cause and its next step. Do not prepare fixes.`;
                  const prompt = `Prepare fixes for all orders in issue group ${group.group_id} (${group.pattern}). First call transaction_ops.group_breakdown with group_id "${group.group_id}"${exactScope}. Then call transaction_ops.accounting_group with the same group_id and scope only if a cause's next step is prepare_corrections; otherwise explain the causes and stop. Prepare supported exact invoice corrections together for human approval; show every unsupported case separately. Execute only after I approve the exact group card, with bounded concurrency and per-order verification and audit. Do not treat this request or the group ID as financial approval.`;
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
                        <div className="flex flex-col gap-1">
                          <Link
                            className="whitespace-nowrap text-primary underline"
                            href={`/chat?${new URLSearchParams({ compose: explain, new_session: "true" })}`}
                          >
                            Explain group →
                          </Link>
                          <Link
                            className="whitespace-nowrap text-primary underline"
                            href={`/chat?${new URLSearchParams({ compose: prompt, new_session: "true" })}`}
                          >
                            Prepare group fixes →
                          </Link>
                        </div>
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
          <Pagination
            offset={offset}
            size={size}
            total={groups.data?.total_groups}
            setOffset={setOffset}
            setSize={setSize}
            label="groups"
          />
          {groups.data && (
            <p className="text-xs text-muted-foreground">
              {groups.data.total_cases} orders across all groups in this view
            </p>
          )}
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
