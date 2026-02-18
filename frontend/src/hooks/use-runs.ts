"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  WorkspaceRun,
  WorkspaceArtifact,
  AssertionDefinition,
  UATReport,
} from "@/lib/types";

export function useRuns(workspaceId: string | null) {
  return useQuery<WorkspaceRun[]>({
    queryKey: ["workspace-runs", workspaceId],
    queryFn: () =>
      apiClient.get<WorkspaceRun[]>(
        `/api/v1/workspaces/${workspaceId}/runs`,
      ),
    enabled: !!workspaceId,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (
        data &&
        data.some((r) => r.status === "queued" || r.status === "running")
      ) {
        return 3000;
      }
      return false;
    },
  });
}

export function useRun(runId: string | null) {
  return useQuery<WorkspaceRun>({
    queryKey: ["workspace-run", runId],
    queryFn: () => apiClient.get<WorkspaceRun>(`/api/v1/runs/${runId}`),
    enabled: !!runId,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (data && (data.status === "queued" || data.status === "running")) {
        return 2000;
      }
      return false;
    },
  });
}

export function useRunArtifacts(runId: string | null) {
  return useQuery<WorkspaceArtifact[]>({
    queryKey: ["run-artifacts", runId],
    queryFn: () =>
      apiClient.get<WorkspaceArtifact[]>(`/api/v1/runs/${runId}/artifacts`),
    enabled: !!runId,
  });
}

export function useTriggerValidate() {
  const queryClient = useQueryClient();
  return useMutation<WorkspaceRun, Error, string>({
    mutationFn: (changesetId: string) =>
      apiClient.post<WorkspaceRun>(
        `/api/v1/changesets/${changesetId}/validate`,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-runs"] });
    },
  });
}

export function useTriggerUnitTests() {
  const queryClient = useQueryClient();
  return useMutation<WorkspaceRun, Error, string>({
    mutationFn: (changesetId: string) =>
      apiClient.post<WorkspaceRun>(
        `/api/v1/changesets/${changesetId}/unit-tests`,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-runs"] });
    },
  });
}

export function useTriggerAssertions() {
  const queryClient = useQueryClient();
  return useMutation<
    WorkspaceRun,
    Error,
    { changesetId: string; assertions: AssertionDefinition[] }
  >({
    mutationFn: ({ changesetId, assertions }) =>
      apiClient.post<WorkspaceRun>(
        `/api/v1/changesets/${changesetId}/suiteql-assertions`,
        { assertions },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-runs"] });
    },
  });
}

export function useTriggerDeploySandbox() {
  const queryClient = useQueryClient();
  return useMutation<
    WorkspaceRun,
    Error,
    {
      changesetId: string;
      overrideReason?: string;
      requireAssertions?: boolean;
    }
  >({
    mutationFn: ({ changesetId, overrideReason, requireAssertions }) =>
      apiClient.post<WorkspaceRun>(
        `/api/v1/changesets/${changesetId}/deploy-sandbox`,
        {
          override_reason: overrideReason,
          require_assertions: requireAssertions ?? false,
        },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-runs"] });
    },
  });
}

export function useUATReport(changesetId: string | null) {
  return useQuery<UATReport>({
    queryKey: ["uat-report", changesetId],
    queryFn: () =>
      apiClient.get<UATReport>(
        `/api/v1/changesets/${changesetId}/uat-report`,
      ),
    enabled: !!changesetId,
  });
}
