"use client";

import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { AuditEvent, PaginatedResponse } from "@/lib/types";

interface UseAuditParams {
  page?: number;
  pageSize?: number;
  category?: string;
  action?: string;
  correlationId?: string;
  startDate?: string;
  endDate?: string;
}

export function useAudit({
  page = 1,
  pageSize = 25,
  category,
  action,
  correlationId,
  startDate,
  endDate,
}: UseAuditParams = {}) {
  const params = new URLSearchParams();
  params.set("page", page.toString());
  params.set("page_size", pageSize.toString());
  if (category) params.set("category", category);
  if (action) params.set("action", action);
  if (correlationId) params.set("correlation_id", correlationId);
  if (startDate) params.set("start_date", startDate);
  if (endDate) params.set("end_date", endDate);

  return useQuery<PaginatedResponse<AuditEvent>>({
    queryKey: [
      "audit",
      page,
      pageSize,
      category,
      action,
      correlationId,
      startDate,
      endDate,
    ],
    queryFn: () =>
      apiClient.get<PaginatedResponse<AuditEvent>>(
        `/api/v1/audit?${params.toString()}`,
      ),
    placeholderData: keepPreviousData,
  });
}
