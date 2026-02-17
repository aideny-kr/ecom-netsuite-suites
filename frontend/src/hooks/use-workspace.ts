"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  Workspace,
  FileTreeNode,
  FileReadResponse,
  SearchResult,
} from "@/lib/types";

export function useWorkspaces() {
  return useQuery<Workspace[]>({
    queryKey: ["workspaces"],
    queryFn: () => apiClient.get<Workspace[]>("/api/v1/workspaces"),
  });
}

export function useWorkspace(id: string | null) {
  return useQuery<Workspace>({
    queryKey: ["workspaces", id],
    queryFn: () => apiClient.get<Workspace>(`/api/v1/workspaces/${id}`),
    enabled: !!id,
  });
}

export function useCreateWorkspace() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { name: string; description?: string }) =>
      apiClient.post<Workspace>("/api/v1/workspaces", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });
}

export function useDeleteWorkspace() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiClient.delete<void>(`/api/v1/workspaces/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });
}

export function useWorkspaceFiles(workspaceId: string | null, prefix?: string) {
  return useQuery<FileTreeNode[]>({
    queryKey: ["workspace-files", workspaceId, prefix],
    queryFn: () => {
      const params = new URLSearchParams();
      if (prefix) params.set("prefix", prefix);
      params.set("recursive", "true");
      return apiClient.get<FileTreeNode[]>(
        `/api/v1/workspaces/${workspaceId}/files?${params}`,
      );
    },
    enabled: !!workspaceId,
  });
}

export function useFileContent(workspaceId: string | null, fileId: string | null) {
  return useQuery<FileReadResponse>({
    queryKey: ["file-content", workspaceId, fileId],
    queryFn: () =>
      apiClient.get<FileReadResponse>(
        `/api/v1/workspaces/${workspaceId}/files/${fileId}`,
      ),
    enabled: !!workspaceId && !!fileId,
  });
}

export function useSearchFiles(workspaceId: string | null, query: string) {
  return useQuery<SearchResult[]>({
    queryKey: ["workspace-search", workspaceId, query],
    queryFn: () =>
      apiClient.get<SearchResult[]>(
        `/api/v1/workspaces/${workspaceId}/search?query=${encodeURIComponent(query)}`,
      ),
    enabled: !!workspaceId && query.length >= 2,
  });
}

export function useImportWorkspace() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      workspaceId,
      file,
    }: {
      workspaceId: string;
      file: File;
    }) => {
      const formData = new FormData();
      formData.append("file", file);
      const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      const token = typeof window !== "undefined" ? localStorage.getItem("access_token") : null;
      const res = await fetch(
        `${BASE_URL}/api/v1/workspaces/${workspaceId}/import`,
        {
          method: "POST",
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          body: formData,
        },
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Import failed");
      }
      return res.json();
    },
    onSuccess: (_, { workspaceId }) => {
      queryClient.invalidateQueries({ queryKey: ["workspace-files", workspaceId] });
    },
  });
}
