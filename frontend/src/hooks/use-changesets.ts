"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { ChangeSet, DiffViewResponse } from "@/lib/types";

export function useChangesets(workspaceId: string | null) {
  return useQuery<ChangeSet[]>({
    queryKey: ["changesets", workspaceId],
    queryFn: () =>
      apiClient.get<ChangeSet[]>(
        `/api/v1/workspaces/${workspaceId}/changesets`,
      ),
    enabled: !!workspaceId,
  });
}

export function useChangeset(changesetId: string | null) {
  return useQuery<ChangeSet>({
    queryKey: ["changeset", changesetId],
    queryFn: () =>
      apiClient.get<ChangeSet>(`/api/v1/changesets/${changesetId}`),
    enabled: !!changesetId,
  });
}

export function useChangesetDiff(changesetId: string | null) {
  return useQuery<DiffViewResponse>({
    queryKey: ["changeset-diff", changesetId],
    queryFn: () =>
      apiClient.get<DiffViewResponse>(`/api/v1/changesets/${changesetId}/diff`),
    enabled: !!changesetId,
  });
}

export function useCreateChangeset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      workspaceId,
      title,
      description,
    }: {
      workspaceId: string;
      title: string;
      description?: string;
    }) =>
      apiClient.post<ChangeSet>(
        `/api/v1/workspaces/${workspaceId}/changesets`,
        { title, description },
      ),
    onSuccess: (_, { workspaceId }) => {
      queryClient.invalidateQueries({ queryKey: ["changesets", workspaceId] });
    },
  });
}

export function useTransitionChangeset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      changesetId,
      action,
      reason,
    }: {
      changesetId: string;
      action: string;
      reason?: string;
    }) =>
      apiClient.post<ChangeSet>(`/api/v1/changesets/${changesetId}/transition`, {
        action,
        rejection_reason: reason,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["changesets"] });
      queryClient.invalidateQueries({ queryKey: ["changeset"] });
    },
  });
}

export function useApplyChangeset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (changesetId: string) =>
      apiClient.post<ChangeSet>(`/api/v1/changesets/${changesetId}/apply`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["changesets"] });
      queryClient.invalidateQueries({ queryKey: ["changeset"] });
      queryClient.invalidateQueries({ queryKey: ["workspace-files"] });
      queryClient.invalidateQueries({ queryKey: ["file-content"] });
    },
  });
}
