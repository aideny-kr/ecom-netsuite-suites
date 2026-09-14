"use client";
import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { useTransactionAccess } from "./use-transaction-ops";
import type { ConfigInput } from "@/components/transaction-ops/setup-input";
import type { TransactionConfig } from "@/components/transaction-ops/types";
export interface StepOption {
  id: string;
  reference_name: string | null;
  flow_name: string;
  integration_name: string;
  connection_label: string;
  sandbox: boolean | null;
  mirrored_at: string | null;
  provider_verified: false;
}
interface SetupOptions {
  source_steps: StepOption[];
  target_steps: StepOption[];
  netsuite_connections: {
    id: string;
    label: string;
    account_id: string | null;
  }[];
  source_has_more: boolean;
  target_has_more: boolean;
  connection_has_more: boolean;
}
const base = "/api/v1/transaction-ops";
export function useTransactionSetupOptions() {
  const access = useTransactionAccess();
  return useInfiniteQuery({
    queryKey: ["transaction-ops", access.tenantId, "setup-options"],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      apiClient.get<SetupOptions>(
        `${base}/setup/options?offset=${pageParam}&limit=100`,
      ),
    getNextPageParam: (page, pages) =>
      page.source_has_more || page.target_has_more || page.connection_has_more
        ? pages.length * 100
        : undefined,
    enabled: access.allowed && access.canManage,
  });
}
export function useCreateTransactionConfig() {
  const access = useTransactionAccess(),
    queries = useQueryClient();
  return useMutation({
    mutationFn: (input: ConfigInput) => {
      if (!access.allowed || !access.canManage)
        throw new Error("Configuration access unavailable");
      return apiClient.post<TransactionConfig>(`${base}/configs`, input);
    },
    onSuccess: () =>
      queries.invalidateQueries({
        queryKey: ["transaction-ops", access.tenantId],
      }),
  });
}
export function useControlTransactionConfig() {
  const access = useTransactionAccess(),
    queries = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      enabled,
      schedule_enabled,
    }: {
      id: string;
      enabled: boolean;
      schedule_enabled: boolean;
    }) => {
      if (!access.allowed || !access.canManage)
        throw new Error("Configuration access unavailable");
      return apiClient.patch<TransactionConfig>(
        `${base}/configs/${encodeURIComponent(id)}`,
        { enabled, schedule_enabled },
      );
    },
    onSettled: () =>
      queries.invalidateQueries({
        queryKey: ["transaction-ops", access.tenantId],
      }),
  });
}
