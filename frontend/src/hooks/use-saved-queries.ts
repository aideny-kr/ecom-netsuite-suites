"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  SavedQueryResponse,
  SavedQueryCreatePayload,
} from "@/types/analytics";

export function useSavedQueries(enabled = true) {
  return useQuery<SavedQueryResponse[]>({
    queryKey: ["saved-queries"],
    queryFn: () => apiClient.get<SavedQueryResponse[]>("/api/v1/skills"),
    enabled,
  });
}

export function useCreateSavedQuery() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: SavedQueryCreatePayload) =>
      apiClient.post<SavedQueryResponse>("/api/v1/skills", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["saved-queries"] });
    },
  });
}

export function useDeleteSavedQuery() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiClient.delete(`/api/v1/skills/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["saved-queries"] });
    },
  });
}
