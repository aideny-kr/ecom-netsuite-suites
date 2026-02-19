"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { Connection } from "@/lib/types";

export function useConnections() {
  return useQuery<Connection[]>({
    queryKey: ["connections"],
    queryFn: () => apiClient.get<Connection[]>("/api/v1/connections"),
  });
}

interface CreateConnectionPayload {
  provider: "shopify" | "stripe" | "netsuite";
  label: string;
  credentials: Record<string, string>;
}

export function useCreateConnection() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: CreateConnectionPayload) =>
      apiClient.post<Connection>("/api/v1/connections", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}

export function useDeleteConnection() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) =>
      apiClient.delete<void>(`/api/v1/connections/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}

interface UpdateConnectionPayload {
  label?: string;
  auth_type?: string;
}

export function useUpdateConnection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: UpdateConnectionPayload }) =>
      apiClient.patch<Connection>(`/api/v1/connections/${id}`, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}

export function useReconnectConnection() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiClient.post<Connection>(`/api/v1/connections/${id}/reconnect`, {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}

export function useTestConnection() {
  return useMutation({
    mutationFn: (id: string) =>
      apiClient.post<{ connection_id: string; status: string; message: string }>(
        `/api/v1/connections/${id}/test`,
        {},
      ),
  });
}
