"use client";

import { useQuery } from "@tanstack/react-query";

import { apiClient } from "@/lib/api-client";
import type { AgentSkillMetadata } from "@/lib/types";

// Shares the React Query cache with chat-input.tsx (same ["agent-skills"] key +
// same catalog endpoint), so opening the Skills page warms the chat composer's
// slash-command menu and vice versa.
export function useAgentSkills() {
  return useQuery<AgentSkillMetadata[]>({
    queryKey: ["agent-skills"],
    queryFn: () => apiClient.get<AgentSkillMetadata[]>("/api/v1/skills/catalog"),
    staleTime: 5 * 60 * 1000,
  });
}
