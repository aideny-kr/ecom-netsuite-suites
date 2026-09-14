"use client";

import { hashKey, useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { PaginatedResponse } from "@/lib/types";
import { useAuth } from "@/providers/auth-provider";

interface UseTableDataParams {
  tableName: string;
  page?: number;
  pageSize?: number;
  sortBy?: string;
  sortOrder?: "asc" | "desc";
  search?: string;
  filters?: Record<string, string>;
  refetchInterval?: number;
}

export function useTableData<T = Record<string, unknown>>({
  tableName,
  page = 1,
  pageSize = 25,
  sortBy,
  sortOrder = "asc",
  search,
  filters = {},
  refetchInterval,
}: UseTableDataParams) {
  const { user } = useAuth();
  const params = new URLSearchParams(filters);
  params.set("page", page.toString());
  params.set("page_size", pageSize.toString());
  if (sortBy) {
    params.set("sort_by", sortBy);
    params.set("sort_order", sortOrder);
  }
  if (search) {
    params.set("search", search);
  }

  return useQuery<PaginatedResponse<T>>({
    queryKey: [
      "table",
      user?.tenant_id,
      tableName,
      page,
      pageSize,
      sortBy,
      sortOrder,
      search,
      filters,
    ],
    enabled: !!user?.tenant_id,
    refetchInterval,
    queryFn: () =>
      apiClient.get<PaginatedResponse<T>>(
        `/api/v1/tables/${tableName}?${params.toString()}`,
      ),
    placeholderData: (previous, query) =>
      query && query.queryKey[1] === user?.tenant_id &&
      query.queryKey[2] === tableName &&
      query.queryKey[7] === search &&
      hashKey([query.queryKey[8]]) === hashKey([filters])
        ? previous
        : undefined,
  });
}
