"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  WorkspaceRun,
  WorkspaceArtifact,
  AssertionDefinition,
  DeployPreview,
  UATReport,
} from "@/lib/types";

export function useRuns(workspaceId: string | null) {
  return useQuery<WorkspaceRun[]>({
    queryKey: ["workspace-runs", workspaceId],
    queryFn: () =>
      apiClient.get<WorkspaceRun[]>(`/api/v1/workspaces/${workspaceId}/runs`),
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

/**
 * Mint a deploy preview + HMAC token. Does NOT queue the deploy run —
 * the caller renders the manifest in a DeployConfirmationCard and waits
 * for the user to confirm via useConfirmDeploy.
 */
export function useDeployPreview() {
  return useMutation<
    DeployPreview,
    Error,
    {
      changesetId: string;
      sandboxId: string;
      requireAssertions?: boolean;
    }
  >({
    mutationFn: ({ changesetId, sandboxId, requireAssertions }) =>
      apiClient.post<DeployPreview>(
        `/api/v1/changesets/${changesetId}/deploy-sandbox/preview`,
        {
          sandbox_id: sandboxId,
          require_assertions: requireAssertions ?? false,
        },
      ),
  });
}

/**
 * Confirm a previously minted deploy preview, queueing the workspace run.
 * Server re-verifies snapshot + gates + HMAC before dispatching to the
 * worker, which re-verifies snapshot once more before suitecloud
 * project:deploy.
 */
export function useConfirmDeploy() {
  const queryClient = useQueryClient();
  return useMutation<
    WorkspaceRun,
    Error,
    { changesetId: string; jti: string; confirmationToken: string }
  >({
    mutationFn: ({ changesetId, jti, confirmationToken }) =>
      apiClient.post<WorkspaceRun>(
        `/api/v1/changesets/${changesetId}/deploy-sandbox/confirm`,
        { jti, confirmation_token: confirmationToken },
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
      apiClient.get<UATReport>(`/api/v1/changesets/${changesetId}/uat-report`),
    enabled: !!changesetId,
  });
}
