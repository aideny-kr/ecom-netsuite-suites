"use client";

import { useMutation } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";

interface MockDataRequest {
  query: string;
  limit?: number;
  mask_pii?: boolean;
}

interface MockDataResult {
  status: string;
  columns: string[];
  data: Record<string, unknown>[];
  row_count: number;
  masked: boolean;
}

export function useMockData() {
  return useMutation({
    mutationFn: (params: MockDataRequest) =>
      apiClient.post<MockDataResult>("/api/v1/netsuite/scripts/mock-data", params),
  });
}
