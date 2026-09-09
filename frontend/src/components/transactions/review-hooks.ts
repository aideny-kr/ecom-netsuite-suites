"use client";
import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useTransactionAccess } from "@/hooks/use-transaction-ops";
import { apiClient } from "@/lib/api-client";
import type {
  JsonObject,
  TransactionProposal,
  TransactionRun,
} from "../transaction-ops/types";
const base = "/api/v1/transaction-ops";
const enc = encodeURIComponent;
export type TransactionCase = {
  id: string;
  order_reference: string;
  status: string;
  scope_json: JsonObject;
  last_observed_at: string;
  latest_report_json: JsonObject;
};
export type ReviewRow = {
  id: string;
  review_run_id?: string;
  config_id?: string;
  run_id: string;
  order_reference: string;
  case_id?: string;
  observed_at: string;
  balance: JsonObject | null;
  action?: string;
};
export type ReviewResults = {
  items: ReviewRow[];
  total: number;
  has_next: boolean;
  summary: {
    checked: number;
    matched: number;
    needs_review: number;
    not_verified: number;
  };
};
export type Coverage = {
  complete: boolean;
  status: string;
  completed_slices: number;
  period_start: string;
  period_end: string;
  completed_until: string;
  current_run_id: string;
};
export type PeriodInput = {
  period: "last_week" | "last_month" | "yesterday" | "custom";
  start_date?: string;
  end_date?: string;
  evaluation_key: string;
};
export type BatchInvestigation = {
  runs: { id: string; config_id: string; case_ids: string[] }[];
  blocked: { case_id: string; code: string }[];
};
export function useReviewRuns() {
  const access = useTransactionAccess();
  return useQuery({
    queryKey: ["transaction-ops", access.tenantId, "review-runs"],
    enabled: access.allowed,
    queryFn: () => apiClient.get<TransactionRun[]>(`${base}/runs?limit=200`),
    refetchInterval: 10000,
  });
}
export function usePeriodData(
  ids: string[],
  offset: number,
  status: string,
  search: string,
  size = 50,
) {
  const access = useTransactionAccess();
  const query = new URLSearchParams({
    offset: String(offset),
    limit: String(size),
    ...(status ? { status } : {}),
    ...(search ? { search } : {}),
  });
  ids.forEach((id) => query.append("review_run_ids", id));
  const result = useQuery({
    queryKey: [
      "transaction-ops",
      access.tenantId,
      "period-results",
      ids,
      offset,
      size,
      status,
      search,
    ],
    enabled: access.allowed && ids.length > 0,
    queryFn: () =>
      apiClient.get<ReviewResults>(`${base}/review-results?${query}`),
    refetchInterval: 30000,
  });
  const results = [result];
  const coverage = useQueries({
    queries: ids.map((id) => ({
      queryKey: ["transaction-ops", access.tenantId, "period-coverage", id],
      enabled: access.allowed,
      queryFn: () => apiClient.get<Coverage>(`${base}/runs/${enc(id)}/review`),
      refetchInterval: 10000,
    })),
  });
  return { results, coverage };
}
export type RecordPage<T> = { items: T[]; total: number; has_next: boolean };
function useRecordPage<T>(
  view: string,
  offset: number,
  size: number,
  enabled: boolean,
  configId = "",
) {
  const access = useTransactionAccess();
  const params = new URLSearchParams({
    view,
    offset: String(offset),
    limit: String(size),
    ...(configId ? { config_id: configId } : {}),
  });
  return useQuery({
    queryKey: [
      "transaction-ops",
      access.tenantId,
      "workspace-page",
      view,
      offset,
      size,
      configId,
    ],
    enabled: access.allowed && enabled,
    queryFn: () =>
      apiClient.get<RecordPage<T>>(`${base}/workspace-page?${params}`),
    refetchInterval: 30000,
  });
}
export function useCases(offset: number, enabled: boolean, size = 50) {
  return useRecordPage<TransactionCase>("cases", offset, size, enabled);
}
export function useRunHistory(
  offset: number,
  enabled: boolean,
  size = 50,
  configId = "",
) {
  return useRecordPage<TransactionRun>("runs", offset, size, enabled, configId);
}
export function useCaseEvidence(id: string) {
  const access = useTransactionAccess();
  const detail = useQuery({
    queryKey: ["transaction-ops", access.tenantId, "case", id],
    enabled: access.allowed && !!id,
    queryFn: () => apiClient.get<TransactionCase>(`${base}/cases/${enc(id)}`),
    refetchInterval: 10000,
  });
  const history = useQuery({
    queryKey: ["transaction-ops", access.tenantId, "case-history", id],
    enabled: access.allowed && !!id,
    queryFn: () =>
      apiClient.get<
        {
          id: string;
          run_id: string;
          observed_at: string;
          report_json: JsonObject;
        }[]
      >(`${base}/cases/${enc(id)}/observations?limit=20`),
    refetchInterval: 10000,
  });
  return { detail, history };
}
export function useFixProposals(offset: number, enabled: boolean, size = 50) {
  return useRecordPage<TransactionProposal>("proposals", offset, size, enabled);
}
export function useStartPeriodReview() {
  const access = useTransactionAccess();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      configId,
      request,
    }: {
      configId: string;
      request: PeriodInput;
    }) => {
      if (!access.allowed) throw new Error("Access unavailable");
      return apiClient.post<TransactionRun>(
        `${base}/configs/${enc(configId)}/review`,
        request,
      );
    },
    onSuccess: () =>
      client.invalidateQueries({
        queryKey: ["transaction-ops", access.tenantId],
      }),
  });
}
export function useBulkCaseInvestigation() {
  const access = useTransactionAccess();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (request: { case_ids: string[]; evaluation_key: string }) => {
      if (!access.allowed) throw new Error("Access unavailable");
      return apiClient.post<BatchInvestigation>(
        `${base}/cases/investigate`,
        request,
      );
    },
    onSuccess: () =>
      client.invalidateQueries({
        queryKey: ["transaction-ops", access.tenantId],
      }),
  });
}
