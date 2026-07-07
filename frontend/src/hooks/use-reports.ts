"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export interface ReportSummary {
  id: string;
  title: string;
  status: string;
  version: number;
  created_at: string;
  /** Slice A: a refresh recipe was captured — the Refresh button renders iff true. */
  has_recipe: boolean;
  /** Slice B: the "data as of" stamp source; null/absent = never refreshed. */
  last_refreshed_at?: string | null;
}

export interface ReportVersionEntry {
  version: number;
  created_at: string;
  created_by?: string | null;
  pinned: boolean;
  /** The version the stable /view URL currently serves. */
  is_current: boolean;
}

export function useReports() {
  return useQuery<ReportSummary[]>({
    queryKey: ["reports"],
    queryFn: () => apiClient.get<ReportSummary[]>("/api/v1/reports"),
  });
}

export function useReport(id: string | null) {
  return useQuery<ReportSummary>({
    queryKey: ["reports", id],
    queryFn: () => apiClient.get<ReportSummary>(`/api/v1/reports/${id}`),
    enabled: !!id,
  });
}

export function useReportVersions(id: string | null) {
  return useQuery<ReportVersionEntry[]>({
    queryKey: ["reports", id, "versions"],
    queryFn: () => apiClient.get<ReportVersionEntry[]>(`/api/v1/reports/${id}/versions`),
    enabled: !!id,
  });
}

export function useRefreshReport(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.post<ReportSummary>(`/api/v1/reports/${id}/refresh`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["reports"] });
      queryClient.invalidateQueries({ queryKey: ["reports", id] });
      queryClient.invalidateQueries({ queryKey: ["reports", id, "versions"] });
    },
  });
}
