"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface AgentInstructions {
  agent_id: string;
  instructions: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export function useAgentInstructions(agentId: string | null) {
  return useQuery<AgentInstructions>({
    queryKey: ["agent-instructions", agentId],
    queryFn: () => apiClient.get<AgentInstructions>(`/api/v1/agents/${agentId}/instructions`),
    enabled: !!agentId,
  });
}

export function useUpdateAgentInstructions(agentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (instructions: string) =>
      apiClient.put<AgentInstructions>(`/api/v1/agents/${agentId}/instructions`, { instructions }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent-instructions", agentId] });
    },
  });
}
