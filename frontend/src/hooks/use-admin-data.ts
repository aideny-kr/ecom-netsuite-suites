"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { AdminTenant, PlatformStats, AdminWallet, ImpersonateResponse } from "@/lib/types";

export function useAdminTenants() {
  return useQuery<AdminTenant[]>({
    queryKey: ["admin", "tenants"],
    queryFn: () => apiClient.get<AdminTenant[]>("/api/v1/admin/tenants"),
  });
}

export function usePlatformStats() {
  return useQuery<PlatformStats>({
    queryKey: ["admin", "stats"],
    queryFn: () => apiClient.get<PlatformStats>("/api/v1/admin/stats"),
  });
}

export function useTenantWallet(tenantId: string | null) {
  return useQuery<AdminWallet | null>({
    queryKey: ["admin", "tenants", tenantId, "wallet"],
    queryFn: () => apiClient.get<AdminWallet | null>(`/api/v1/admin/tenants/${tenantId}/wallet`),
    enabled: !!tenantId,
  });
}

export function useImpersonateTenant() {
  return useMutation({
    mutationFn: (tenantId: string) =>
      apiClient.post<ImpersonateResponse>(`/api/v1/admin/tenants/${tenantId}/impersonate`),
  });
}

export function useUpdateWallet() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ tenantId, data }: { tenantId: string; data: Partial<AdminWallet> }) =>
      apiClient.patch<AdminWallet>(`/api/v1/admin/tenants/${tenantId}/wallet`, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin"] });
    },
  });
}
