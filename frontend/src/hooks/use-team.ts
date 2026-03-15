"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface TeamMember {
  id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  roles: string[];
}

interface TeamInvite {
  id: string;
  email: string;
  role_name: string;
  role_display_name: string;
  status: string;
  expires_at: string;
  created_at: string;
}

export type { TeamMember, TeamInvite };

export function useTeamMembers() {
  return useQuery<TeamMember[]>({
    queryKey: ["team", "members"],
    queryFn: () => apiClient.get("/api/v1/users"),
  });
}

export function useTeamInvites() {
  return useQuery<TeamInvite[]>({
    queryKey: ["team", "invites"],
    queryFn: () => apiClient.get("/api/v1/invites"),
  });
}

export function useCreateInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { email: string; role_name: string }) =>
      apiClient.post("/api/v1/invites", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}

export function useRevokeInvite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (inviteId: string) =>
      apiClient.delete(`/api/v1/invites/${inviteId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}

export function useChangeUserRole() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, roles }: { userId: string; roles: string[] }) =>
      apiClient.patch(`/api/v1/users/${userId}/roles`, { role_names: roles }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}

export function useDeactivateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) =>
      apiClient.delete(`/api/v1/users/${userId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team"] }),
  });
}
