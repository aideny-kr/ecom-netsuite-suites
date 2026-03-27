"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export interface CurrencyConfig {
  fx_rate: number;
  tier: "usd_based" | "eur_based";
  vat_rate: number | null;
  rounding_rule: "nearest_9" | "nearest_100" | "nearest_990" | "nearest_50" | "no_rounding";
}

export interface TenantPricingConfig {
  version: number;
  base_currency: string;
  currencies: Record<string, CurrencyConfig>;
  eur_fx_rate: number;
}

export interface PricingConfigResponse {
  id: string;
  tenant_id: string;
  config: TenantPricingConfig;
  updated_by: string | null;
  created_at: string;
  updated_at: string;
}

export function usePricingConfig() {
  return useQuery<PricingConfigResponse>({
    queryKey: ["pricing-config"],
    queryFn: () => apiClient.get<PricingConfigResponse>("/api/v1/pricing-config"),
  });
}

export function useUpdatePricingConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (config: TenantPricingConfig) =>
      apiClient.put<PricingConfigResponse>("/api/v1/pricing-config", { config }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pricing-config"] });
    },
  });
}
