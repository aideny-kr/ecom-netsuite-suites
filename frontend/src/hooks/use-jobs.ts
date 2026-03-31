"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface JobHistoryItem {
  id: string;
  job_type: string;
  status: string;
  result_summary: Record<string, unknown> | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  celery_task_id: string | null;
}

interface PaginatedJobs {
  items: JobHistoryItem[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

interface ScheduleItem {
  name: string;
  task: string;
  schedule: string;
  enabled: boolean;
}

export type { JobHistoryItem, ScheduleItem };

export function useJobHistory(pageSize = 10, status?: string) {
  return useQuery<PaginatedJobs>({
    queryKey: ["jobs", pageSize, status],
    queryFn: () => {
      const params = new URLSearchParams({ page: "1", page_size: String(pageSize) });
      if (status) params.set("status", status);
      return apiClient.get<PaginatedJobs>(`/api/v1/jobs?${params}`);
    },
    refetchInterval: 15_000,
  });
}

export function useJobSchedules() {
  return useQuery<ScheduleItem[]>({
    queryKey: ["jobs", "schedules"],
    queryFn: () => apiClient.get("/api/v1/jobs/schedules"),
  });
}

export function useTriggerJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskName: string) =>
      apiClient.post(`/api/v1/jobs/trigger/${taskName}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
}
