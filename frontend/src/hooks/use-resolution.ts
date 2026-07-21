"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  ReconResolutionProposal,
  ReconResolutionSummary,
} from "@/lib/types";

export function useResolutionSummary(runId: string | null) {
  return useQuery<ReconResolutionSummary>({
    queryKey: ["recon-resolution-summary", runId],
    queryFn: () =>
      apiClient.get<ReconResolutionSummary>(
        `/api/v1/reconciliation/runs/${runId}/resolution-summary`
      ),
    enabled: !!runId,
    refetchInterval: (query) =>
      query.state.data?.agent_job?.status === "running" ? 5000 : false,
  });
}

export function useGroupProposals(
  runId: string | null,
  groupKey: string | null
) {
  return useQuery<ReconResolutionProposal[]>({
    queryKey: ["recon-group-proposals", runId, groupKey],
    queryFn: () =>
      apiClient.get<ReconResolutionProposal[]>(
        `/api/v1/reconciliation/runs/${runId}/resolution-groups/${encodeURIComponent(
          groupKey!
        )}/proposals`
      ),
    enabled: !!runId && !!groupKey,
  });
}

// The route defaults to limit=100 server-side, which would silently
// truncate the cross-group needs-human worksheet on a busy run. Request a
// high ceiling explicitly; the worksheet compares the returned count against
// this same constant to warn the operator if even THAT ceiling was hit.
export const NEEDS_HUMAN_PROPOSALS_LIMIT = 1000;

// The group_key-less route filters cross-group by action alone — the
// needs-human worksheet spans every root_cause/booking_vehicle combination
// sharing that action in one call instead of one fetch per group.
export function useNeedsHumanProposals(runId: string | null) {
  return useQuery<ReconResolutionProposal[]>({
    queryKey: ["recon-needs-human-proposals", runId],
    queryFn: () =>
      apiClient.get<ReconResolutionProposal[]>(
        `/api/v1/reconciliation/runs/${runId}/resolution-groups/proposals?action=needs_human&limit=${NEEDS_HUMAN_PROPOSALS_LIMIT}`
      ),
    enabled: !!runId,
  });
}

function invalidateResolution(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: ["recon-resolution-summary"] });
  queryClient.invalidateQueries({ queryKey: ["recon-group-proposals"] });
  queryClient.invalidateQueries({ queryKey: ["recon-needs-human-proposals"] });
  queryClient.invalidateQueries({ queryKey: ["recon-results"] });
  queryClient.invalidateQueries({ queryKey: ["recon-bucket-summary"] });
  // Group approval flips result statuses → the period readiness changes.
  queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
}

export function useApproveResolutionGroup(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      group_key: string;
      notes?: string;
      included_above_materiality_ids?: string[];
      excluded_ids?: string[];
      // Scopes the approve to one currency's card — a group_key alone can
      // now span more than one currency (multi-currency runs render one
      // card per currency).
      currency?: string;
    }) =>
      apiClient.post(
        `/api/v1/reconciliation/runs/${runId}/resolution-groups/${encodeURIComponent(
          data.group_key
        )}/approve`,
        {
          notes: data.notes,
          included_above_materiality_ids: data.included_above_materiality_ids ?? [],
          excluded_ids: data.excluded_ids ?? [],
          currency: data.currency,
        }
      ),
    onSuccess: () => invalidateResolution(queryClient),
  });
}

export function useRejectResolutionGroup(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { group_key: string; currency?: string }) =>
      apiClient.post(
        `/api/v1/reconciliation/runs/${runId}/resolution-groups/${encodeURIComponent(
          data.group_key
        )}/reject`,
        { currency: data.currency }
      ),
    onSuccess: () => invalidateResolution(queryClient),
  });
}

export function usePlanResolutions(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiClient.post(
        `/api/v1/reconciliation/runs/${runId}/plan-resolutions`,
        {}
      ),
    onSuccess: () => invalidateResolution(queryClient),
  });
}
