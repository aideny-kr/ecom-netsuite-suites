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
) {
  const access = useTransactionAccess();
  const results = useQueries({
    queries: ids.map((id) => ({
      queryKey: [
        "transaction-ops",
        access.tenantId,
        "period-results",
        id,
        offset,
        status,
        search,
      ],
      enabled: access.allowed,
      queryFn: () =>
        apiClient.get<ReviewResults>(
          `${base}/runs/${enc(id)}/review/findings?${new URLSearchParams({ offset: String(offset), limit: "25", ...(status ? { status } : {}), ...(search ? { search } : {}) })}`,
        ),
      refetchInterval: 10000,
    })),
  });
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
export function useCases(offset: number, enabled: boolean) {
  const access = useTransactionAccess();
  return useQuery({
    queryKey: ["transaction-ops", access.tenantId, "cases", offset],
    enabled: access.allowed && enabled,
    queryFn: () =>
      apiClient.get<TransactionCase[]>(
        `${base}/cases?status=open&limit=51&offset=${offset}`,
      ),
    refetchInterval: 10000,
  });
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
export function useFixProposals(offset: number, enabled: boolean) {
  const access = useTransactionAccess();
  return useQuery({
    queryKey: ["transaction-ops", access.tenantId, "fix-proposals", offset],
    enabled: access.allowed && enabled,
    queryFn: () =>
      apiClient.get<TransactionProposal[]>(
        `${base}/proposals?limit=21&offset=${offset}`,
      ),
    refetchInterval: 10000,
  });
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
