"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { PlanInfo } from "@/lib/types";

export function usePlanInfo() {
  return useQuery<PlanInfo>({
    queryKey: ["plan-info"],
    queryFn: () => apiClient.get<PlanInfo>("/api/v1/tenants/me/plan"),
  });
}
