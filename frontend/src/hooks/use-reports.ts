"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export type AutoRefreshInterval = "off" | "hourly" | "daily";

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
  /** Slice C: the user-chosen sweep interval (selector renders iff has_recipe). */
  auto_refresh?: AutoRefreshInterval;
  /** Slice C: consecutive failed auto-refreshes — > 0 drives the staleness banner. */
  refresh_failure_count?: number;
  /** Slice C: set = auto-refresh paused after repeated failures (banner + Resume). */
  auto_refresh_paused_at?: string | null;
  /** Creator-or-admin gate for delete/pin — mirrored client-side by canManage(). */
  created_by?: string | null;
  /** Set when pinned to the dashboard landing page; sorts pinned reports newest-first. */
  dashboard_pinned_at?: string | null;
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

export function useUpdateReportSettings(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (auto_refresh: AutoRefreshInterval) =>
      apiClient.patch<ReportSummary>(`/api/v1/reports/${id}/settings`, { auto_refresh }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["reports"] });
      queryClient.invalidateQueries({ queryKey: ["reports", id] });
    },
  });
}

export function useResumeAutoRefresh(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.post<ReportSummary>(`/api/v1/reports/${id}/auto-refresh/resume`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["reports"] });
      queryClient.invalidateQueries({ queryKey: ["reports", id] });
    },
  });
}

export function useDeleteReport(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.delete<void>(`/api/v1/reports/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["reports"] });
    },
  });
}

export interface PlaybookParam {
  key: string;
  label: string;
  example: string;
}

export interface PlaybookInfo {
  key: string;
  name: string;
  description: string;
  params: PlaybookParam[];
}

export function usePlaybooks() {
  return useQuery<PlaybookInfo[]>({
    queryKey: ["report-playbooks"],
    queryFn: () => apiClient.get<PlaybookInfo[]>("/api/v1/reports/playbooks"),
  });
}

export function useComposePlaybook() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (args: { key: string; params: Record<string, string> }) =>
      apiClient.post<ReportSummary>(`/api/v1/reports/playbooks/${args.key}`, { params: args.params }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["reports"] }),
  });
}
