"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "@/lib/api-client";

// Mirrors backend `MemoryConceptResponse` (app/schemas/tenant_memory.py). Every
// UUID is a string and `confidence` is a float (the router's `_*_to_response`
// helpers coerce them). LOCKED field names: name / summary / concept_type /
// review_state — never `label` / `body`.
export type MemoryReviewState = "pending" | "confirmed" | "rejected" | "merged";

export interface MemoryConcept {
  id: string;
  tenant_id: string;
  name: string;
  summary: string;
  concept_type: string | null;
  review_state: MemoryReviewState;
  confidence: number | null;
  confirmed_by: string | null;
  merged_into_id: string | null;
  use_count: number;
  created_at: string;
  updated_at: string;
}

// Mirrors backend `MemoryEdgeResponse`.
export interface MemoryEdge {
  id: string;
  tenant_id: string;
  source_concept_id: string;
  target_concept_id: string;
  relation: string;
  review_state: MemoryReviewState;
  created_at: string;
  updated_at: string;
}

// Mirrors backend `MemoryGraphResponse` — the GET returns an OBJECT with
// `concepts` + `edges`, NOT a bare array.
export interface MemoryGraph {
  concepts: MemoryConcept[];
  edges: MemoryEdge[];
}

// Mirrors backend `MemoryConceptUpdate` — every field optional; only `pending`,
// `confirmed`, `rejected` are accepted for review_state (NOT `merged`).
export interface UpdateConceptPayload {
  name?: string;
  summary?: string;
  concept_type?: string | null;
  review_state?: "pending" | "confirmed" | "rejected";
}

// Mirrors backend `MemoryMergeRequest`.
export interface MergeConceptsPayload {
  survivor_id: string;
  merged_ids: string[];
}

const QUERY_KEY = ["memory-graph"];

export function useMemoryGraph(reviewState?: MemoryReviewState) {
  const query = reviewState ? `?review_state=${reviewState}` : "";
  return useQuery<MemoryGraph>({
    // Scope the cache by review_state so switching filters refetches the right
    // server-side slice rather than reusing a stale one.
    queryKey: [...QUERY_KEY, reviewState ?? "all"],
    queryFn: () => apiClient.get<MemoryGraph>(`/api/v1/tenant-memory${query}`),
  });
}

export function useUpdateConceptReview() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...payload }: UpdateConceptPayload & { id: string }) =>
      apiClient.patch<MemoryConcept>(`/api/v1/tenant-memory/concepts/${id}`, payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}

export function useDeleteConcept() {
  const queryClient = useQueryClient();
  return useMutation({
    // DELETE soft-rejects (flips review_state to 'rejected'); backend returns 204.
    mutationFn: (id: string) =>
      apiClient.delete<void>(`/api/v1/tenant-memory/concepts/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}

export function useMergeConcepts() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: MergeConceptsPayload) =>
      apiClient.post<MemoryConcept>("/api/v1/tenant-memory/concepts/merge", payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });
}
