"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

export interface ReportSummary {
  id: string;
  title: string;
  status: string;
  version: number;
  created_at: string;
}

export function useReports() {
  return useQuery<ReportSummary[]>({
    queryKey: ["reports"],
    queryFn: () => apiClient.get<ReportSummary[]>("/api/v1/reports"),
  });
}
