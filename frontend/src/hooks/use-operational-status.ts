"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient, ApiError } from "@/lib/api-client";
import { useTransactionAccess } from "./use-transaction-ops";
import type { OperationalStatus } from "@/components/transaction-ops/operational-status-types";

export function useOperationalStatus(offset: number) {
  const access = useTransactionAccess();
  return useQuery({
    queryKey: ["transaction-ops", access.tenantId, "operational-status", offset],
    queryFn: () => apiClient.get<OperationalStatus>(
      `/api/v1/transaction-ops/operational-status?limit=20&offset=${offset}`,
    ),
    enabled: access.allowed,
    staleTime: 30_000,
    retry: (count, error) => !(error instanceof ApiError && [401, 403].includes(error.status)) && count < 1,
  });
}
