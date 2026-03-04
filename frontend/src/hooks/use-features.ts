"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { useAuth } from "@/providers/auth-provider";

interface FeatureFlagsResponse {
  flags: Record<string, boolean>;
}

export function useFeatures() {
  const { user } = useAuth();

  return useQuery<Record<string, boolean>>({
    queryKey: ["features"],
    queryFn: async () => {
      const res = await apiClient.get<FeatureFlagsResponse>(
        "/api/v1/settings/features",
      );
      return res.flags;
    },
    enabled: !!user,
    staleTime: 60_000,
  });
}

export function useFeature(key: string): boolean {
  const { data } = useFeatures();
  return data?.[key] ?? false;
}
