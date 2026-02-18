"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  NetSuiteMetadata,
  NetSuiteMetadataFieldsResponse,
  MetadataDiscoveryTaskResponse,
} from "@/lib/types";

/**
 * Fetch the latest discovered metadata summary for the current tenant.
 * Polls every 5s while status is "pending" (discovery in progress).
 */
export function useNetSuiteMetadata() {
  return useQuery<NetSuiteMetadata>({
    queryKey: ["netsuite-metadata"],
    queryFn: () => apiClient.get<NetSuiteMetadata>("/api/v1/netsuite/metadata"),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (data && data.status === "pending") {
        return 5000; // Poll every 5s while discovery is running
      }
      return false;
    },
  });
}

/**
 * Fetch raw field data for a specific metadata category.
 */
export function useMetadataFields(category: string | null) {
  return useQuery<NetSuiteMetadataFieldsResponse>({
    queryKey: ["netsuite-metadata-fields", category],
    queryFn: () =>
      apiClient.get<NetSuiteMetadataFieldsResponse>(
        `/api/v1/netsuite/metadata/fields/${category}`,
      ),
    enabled: !!category,
  });
}

/**
 * Trigger a new metadata discovery run.
 * Invalidates the metadata query so it starts polling for results.
 */
export function useTriggerMetadataDiscovery() {
  const queryClient = useQueryClient();
  return useMutation<MetadataDiscoveryTaskResponse, Error>({
    mutationFn: () =>
      apiClient.post<MetadataDiscoveryTaskResponse>(
        "/api/v1/netsuite/metadata/discover",
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["netsuite-metadata"] });
    },
  });
}
