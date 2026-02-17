"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface AiConfig {
  ai_provider: string | null;
  ai_model: string | null;
  ai_api_key_set: boolean;
}

interface UpdateAiSettingsPayload {
  ai_provider?: string | null;
  ai_model?: string | null;
  ai_api_key?: string | null;
}

interface TestAiKeyPayload {
  provider: string;
  api_key: string;
  model?: string;
}

interface TestAiKeyResponse {
  valid: boolean;
  error?: string;
}

export function useAiSettings() {
  return useQuery<AiConfig>({
    queryKey: ["ai-settings"],
    queryFn: async () => {
      const config = await apiClient.get<Record<string, unknown>>(
        "/api/v1/tenants/me/config",
      );
      return {
        ai_provider: (config.ai_provider as string) ?? null,
        ai_model: (config.ai_model as string) ?? null,
        ai_api_key_set: (config.ai_api_key_set as boolean) ?? false,
      };
    },
  });
}

export function useUpdateAiSettings() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: UpdateAiSettingsPayload) =>
      apiClient.patch<Record<string, unknown>>(
        "/api/v1/tenants/me/config",
        data,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["ai-settings"] });
    },
  });
}

export function useTestAiKey() {
  return useMutation({
    mutationFn: (data: TestAiKeyPayload) =>
      apiClient.post<TestAiKeyResponse>(
        "/api/v1/tenants/me/config/test-ai-key",
        data,
      ),
  });
}
