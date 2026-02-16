"use client";

import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { PaginatedResponse } from "@/lib/types";

interface UseTableDataParams {
  tableName: string;
  page?: number;
  pageSize?: number;
  sortBy?: string;
  sortOrder?: "asc" | "desc";
  search?: string;
}

export function useTableData<T = Record<string, unknown>>({
  tableName,
  page = 1,
  pageSize = 25,
  sortBy,
  sortOrder = "asc",
  search,
}: UseTableDataParams) {
  const params = new URLSearchParams();
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
    queryKey: ["table", tableName, page, pageSize, sortBy, sortOrder, search],
    queryFn: () =>
      apiClient.get<PaginatedResponse<T>>(
        `/api/v1/tables/${tableName}?${params.toString()}`,
      ),
    placeholderData: keepPreviousData,
  });
}
