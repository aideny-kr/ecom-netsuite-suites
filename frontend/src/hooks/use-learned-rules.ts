"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "@/lib/api-client";

export interface LearnedRule {
  id: string;
  tenant_id: string;
  rule_category: string | null;
  rule_description: string;
  is_active: boolean;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateLearnedRulePayload {
  rule_description: string;
  rule_category?: string | null;
}

export interface UpdateLearnedRulePayload {
  rule_description?: string;
  rule_category?: string | null;
  is_active?: boolean;
}

const QUERY_KEY = ["learned-rules"];

export function useLearnedRules() {
  return useQuery<LearnedRule[]>({
    queryKey: QUERY_KEY,
    queryFn: () => apiClient.get<LearnedRule[]>("/api/v1/learned-rules"),
  });
}

export function useCreateLearnedRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreateLearnedRulePayload) =>
      apiClient.post<LearnedRule>("/api/v1/learned-rules", payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}

export function useUpdateLearnedRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...payload }: UpdateLearnedRulePayload & { id: string }) =>
      apiClient.patch<LearnedRule>(`/api/v1/learned-rules/${id}`, payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}

export function useDeleteLearnedRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiClient.delete<void>(`/api/v1/learned-rules/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}
