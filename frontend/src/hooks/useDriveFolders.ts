"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export interface DriveFolder {
  id: string;
  tenant_id: string;
  folder_id: string;
  folder_name: string;
  is_enabled: boolean;
  sync_status: "idle" | "syncing" | "success" | "error";
  last_synced_at: string | null;
  last_sync_error: string | null;
  chunk_count: number;
  file_count: number;
  created_at: string;
}

export function useDriveFolders() {
  return useQuery<DriveFolder[]>({
    queryKey: ["drive-folders"],
    queryFn: () => apiClient.get<DriveFolder[]>("/api/v1/drive-folders"),
  });
}

export function useAddDriveFolder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (folder_id_or_url: string) =>
      apiClient.post<DriveFolder>("/api/v1/drive-folders", { folder_id_or_url }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["drive-folders"] });
    },
  });
}

export function useRemoveDriveFolder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiClient.delete<void>(`/api/v1/drive-folders/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["drive-folders"] });
    },
  });
}

export function useToggleDriveFolder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, is_enabled }: { id: string; is_enabled: boolean }) =>
      apiClient.patch<DriveFolder>(`/api/v1/drive-folders/${id}`, { is_enabled }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["drive-folders"] });
    },
  });
}

export function useSyncDriveFolder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiClient.post<{ accepted: boolean }>(`/api/v1/drive-folders/${id}/sync`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["drive-folders"] });
    },
  });
}
