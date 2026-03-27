"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export interface AgentSummary {
  agent_id: string;
  display_name: string;
  description: string;
}

export function useAgents() {
  return useQuery<AgentSummary[]>({
    queryKey: ["agents"],
    queryFn: () => apiClient.get<AgentSummary[]>("/api/v1/agents"),
    staleTime: 5 * 60 * 1000,
  });
}
