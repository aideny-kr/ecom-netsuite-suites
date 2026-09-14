"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { useAuth } from "@/providers/auth-provider";
import { useFeatures } from "./use-features";
import { usePermissions } from "./use-permissions";
import type {
  DecisionInput,
  RunScope,
  TransactionConfig,
  TransactionFinding,
  TransactionOperation,
  TransactionProposal,
  TransactionRun,
} from "@/components/transaction-ops/types";
const base = "/api/v1/transaction-ops";
const encode = encodeURIComponent;
export function useTransactionAccess() {
  const { user } = useAuth();
  const features = useFeatures();
  const { hasPermission } = usePermissions();
  return {
    tenantId: user?.tenant_id,
    canManage: hasPermission("connections.manage"),
    loading: features.isLoading,
    error: features.error,
    allowed:
      !!user &&
      features.data?.celigo === true &&
      features.data?.reconciliation === true &&
      hasPermission("recon.run"),
  };
}
export function useTransactionConfigs() {
  const access = useTransactionAccess();
  return useQuery({
    queryKey: ["transaction-ops", access.tenantId, "configs"],
    queryFn: () => apiClient.get<TransactionConfig[]>(`${base}/configs`),
    enabled: access.allowed,
  });
}
export function useTransactionRuns(configId: string) {
  const access = useTransactionAccess();
  return useQuery({
    queryKey: ["transaction-ops", access.tenantId, "runs", configId],
    queryFn: () =>
      apiClient.get<TransactionRun[]>(
        `${base}/runs?config_id=${encode(configId)}&limit=100`,
      ),
    enabled: access.allowed && !!configId,
    refetchInterval: 5000,
  });
}
export function useTransactionRun(id: string) {
  const access = useTransactionAccess();
  return useQuery({
    queryKey: ["transaction-ops", access.tenantId, "run", id],
    queryFn: () => apiClient.get<TransactionRun>(`${base}/runs/${encode(id)}`),
    enabled: access.allowed && !!id,
    refetchInterval: (q) =>
      q.state.data?.status === "finished" ? false : 5000,
  });
}
async function pageOf<T>(path: string, offset: number) {
  const items = await apiClient.get<T[]>(
    `${path}${path.includes("?") ? "&" : "?"}offset=${offset}&limit=100`,
  );
  const next =
    items.length === 100
      ? await apiClient.get<T[]>(
          `${path}${path.includes("?") ? "&" : "?"}offset=${offset + 100}&limit=1`,
        )
      : [];
  return { items, hasNext: next.length > 0 };
}
export function useTransactionFindings(runId: string, offset: number) {
  const access = useTransactionAccess();
  return useQuery({
    queryKey: ["transaction-ops", access.tenantId, "findings", runId, offset],
    queryFn: () =>
      pageOf<TransactionFinding>(
        `${base}/runs/${encode(runId)}/findings`,
        offset,
      ),
    enabled: access.allowed && !!runId,
    refetchInterval: 5000,
  });
}
export function useTransactionProposals(runId: string, offset: number) {
  const access = useTransactionAccess();
  return useQuery({
    queryKey: ["transaction-ops", access.tenantId, "proposals", runId, offset],
    queryFn: () =>
      pageOf<TransactionProposal>(
        `${base}/proposals?run_id=${encode(runId)}`,
        offset,
      ),
    enabled: access.allowed && !!runId,
    refetchInterval: 5000,
  });
}
export function useTransactionOperation(id: string, status: string) {
  const access = useTransactionAccess();
  return useQuery({
    queryKey: ["transaction-ops", access.tenantId, "operation", id],
    queryFn: () =>
      apiClient.get<TransactionOperation | null>(
        `${base}/proposals/${encode(id)}/operation`,
      ),
    enabled: access.allowed && status === "approved",
    refetchInterval: (q) =>
      ["verified", "failed"].includes(q.state.data?.status || "")
        ? false
        : 5000,
  });
}
export function useStartTransactionRun() {
  const access = useTransactionAccess();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      configId,
      evaluation_key,
      ...scope
    }: { configId: string; evaluation_key: string } & RunScope) => {
      if (!access.allowed) throw new Error("Access unavailable");
      return apiClient.post<TransactionRun>(
        `${base}/configs/${encode(configId)}/runs`,
        { origin: "manual", evaluation_key, ...scope },
      );
    },
    onSuccess: () =>
      client.invalidateQueries({
        queryKey: ["transaction-ops", access.tenantId],
      }),
  });
}
export function useTransactionDecision() {
  const access = useTransactionAccess();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      decision,
      evidence_fingerprint,
      note,
    }: DecisionInput) => {
      if (!access.allowed) throw new Error("Access unavailable");
      return apiClient.post<TransactionProposal>(
        `${base}/proposals/${encode(id)}/decision`,
        { decision, evidence_fingerprint, ...(note ? { note } : {}) },
      );
    },
    onSettled: () =>
      client.invalidateQueries({
        queryKey: ["transaction-ops", access.tenantId],
      }),
  });
}

export function useRecheckTransactionOperation() {
  const access = useTransactionAccess();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, evaluation_key }: { id: string; evaluation_key: string }) => {
      if (!access.allowed) throw new Error("Access unavailable");
      return apiClient.post<TransactionRun>(
        `${base}/proposals/${encode(id)}/recheck`,
        { evaluation_key },
      );
    },
    onSuccess: () => client.invalidateQueries({ queryKey: ["transaction-ops", access.tenantId] }),
  });
}
