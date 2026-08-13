"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type {
  ReconRun,
  ReconResult,
  ReconRunSummary,
  ReconBucketSummary,
  ReconCloseReadiness,
} from "@/lib/types";

export function useReconRuns() {
  return useQuery<ReconRun[]>({
    queryKey: ["recon-runs"],
    queryFn: () => apiClient.get<ReconRun[]>("/api/v1/reconciliation/runs"),
  });
}

export function useReconResults(
  runId: string | null,
  statusFilter?: string,
  bucket?: string
) {
  const params = new URLSearchParams();
  if (statusFilter) params.set("status_filter", statusFilter);
  if (bucket) params.set("bucket", bucket);

  return useQuery<ReconResult[]>({
    queryKey: ["recon-results", runId, statusFilter, bucket],
    queryFn: () =>
      apiClient.get<ReconResult[]>(
        `/api/v1/reconciliation/runs/${runId}/results?${params.toString()}`
      ),
    enabled: !!runId,
  });
}

export function useCreateReconRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { date_from: string; date_to: string; subsidiary_id?: string }) =>
      apiClient.post<ReconRunSummary>("/api/v1/reconciliation/runs", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["recon-runs"] });
      // R4-A #3: a NEW run changes the period close scope + readiness counts
      // the CloseChecklist gates on (its unreviewed rows count immediately).
      // Without this, a green checklist goes green-STALE and could gate a
      // close that freezes the new run's rows. Bucket summary for symmetry.
      queryClient.invalidateQueries({ queryKey: ["recon-bucket-summary"] });
      queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
    },
  });
}

export function useApproveResult() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { result_id: string; notes?: string }) =>
      apiClient.patch<ReconResult>(
        `/api/v1/reconciliation/results/${data.result_id}/approve`,
        data
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["recon-results"] });
      // A single-row approve changes the bucket counts AND the period
      // close-readiness counts the CloseChecklist gates on (prefix match
      // invalidates every run's summary / every period's readiness).
      queryClient.invalidateQueries({ queryKey: ["recon-bucket-summary"] });
      queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
      // Approve makes a result terminal exactly as reject does, and the resolution
      // surfaces gate their reject control on that terminal status. Giving these keys
      // to reject alone left an approve taken on the classic table showing a stale
      // pending row over there — still offering Reject, whose every click is then a
      // guaranteed 400. Same staleness, opposite verb.
      queryClient.invalidateQueries({ queryKey: ["recon-group-proposals"] });
      queryClient.invalidateQueries({ queryKey: ["recon-needs-human-proposals"] });
      queryClient.invalidateQueries({ queryKey: ["recon-resolution-summary"] });
    },
  });
}

/** Record that a matched row is WRONG (or right but unactionable).
 *
 * The negative half of the review loop. Approve-only feedback cannot tell a row a
 * human walked away from from one never reviewed, so without this the matcher's
 * false-positive rate has no numerator — and unattended posting is gated on that rate.
 */
export function useRejectResult() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { result_id: string; reason: string; note?: string }) =>
      apiClient.patch<ReconResult>(
        `/api/v1/reconciliation/results/${data.result_id}/reject`,
        // Only reason and note go in the body. `result_id` is the path param — the
        // endpoint deliberately does not accept it in the body (the older approve
        // schema does, then ignores it), because two sources for one identity invites
        // a request whose path and body disagree. `note` is omitted rather than sent
        // as "" so that "not supplied" stays distinguishable from "supplied blank",
        // which is the case reason='other' is refused on.
        data.note ? { reason: data.reason, note: data.note } : { reason: data.reason }
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["recon-results"] });
      // A reject moves the row out of its bucket and changes the period readiness the
      // CloseChecklist gates on — same reason approve invalidates all three.
      queryClient.invalidateQueries({ queryKey: ["recon-bucket-summary"] });
      queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
      // The resolution surfaces render from their OWN keys, and this hook is now
      // shared by all three. Without these a reject taken on the summary-first
      // surface changed nothing on screen — not on refetch, not on a full reload,
      // because the proposals endpoint filters on proposal status while the reject
      // only touches the result. The reviewer could not tell rejected from
      // unrejected on the one surface they use, which is the exact blindness this
      // feature exists to remove.
      queryClient.invalidateQueries({ queryKey: ["recon-group-proposals"] });
      queryClient.invalidateQueries({ queryKey: ["recon-needs-human-proposals"] });
      queryClient.invalidateQueries({ queryKey: ["recon-resolution-summary"] });
    },
  });
}

export function useClosePeriod() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (period: string) =>
      apiClient.post(`/api/v1/reconciliation/close/${period}`, {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["recon-runs"] });
      // Close locks rows server-side (status -> locked): the results table,
      // the bucket counts and the period readiness must refetch too.
      queryClient.invalidateQueries({ queryKey: ["recon-results"] });
      queryClient.invalidateQueries({ queryKey: ["recon-bucket-summary"] });
      queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
      // 'locked' is terminal, so close is the FOURTH mutation of this family —
      // approve, bulk approve, reject, close — and the last one I missed. A worksheet
      // open in another tab when the period closes would keep showing live Reject
      // buttons on rows the API can now only refuse. Every mutation that can make a
      // result terminal has to invalidate these three; there are no others.
      queryClient.invalidateQueries({ queryKey: ["recon-group-proposals"] });
      queryClient.invalidateQueries({ queryKey: ["recon-needs-human-proposals"] });
      queryClient.invalidateQueries({ queryKey: ["recon-resolution-summary"] });
    },
  });
}

export function useReconBucketSummary(runId: string | null) {
  return useQuery<ReconBucketSummary>({
    queryKey: ["recon-bucket-summary", runId],
    queryFn: () =>
      apiClient.get<ReconBucketSummary>(
        `/api/v1/reconciliation/runs/${runId}/buckets`
      ),
    enabled: !!runId,
  });
}

/** PERIOD-scoped close readiness (R3-A): POST /close/{period} closes EVERY
 *  completed run in the month, so the CloseChecklist gate must aggregate over
 *  that same scope — never the selected run's bucket summary. */
export function useCloseReadiness(period: string | null) {
  return useQuery<ReconCloseReadiness>({
    queryKey: ["recon-close-readiness", period],
    queryFn: () =>
      apiClient.get<ReconCloseReadiness>(
        `/api/v1/reconciliation/close-readiness/${period}`
      ),
    enabled: !!period,
  });
}

export function useApproveBucket(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { bucket: string; notes?: string }) =>
      apiClient.post(
        `/api/v1/reconciliation/runs/${runId}/approve-bucket`,
        data
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["recon-results"] });
      queryClient.invalidateQueries({
        queryKey: ["recon-bucket-summary", runId],
      });
      queryClient.invalidateQueries({ queryKey: ["recon-runs"] });
      // Bulk approve drains suggested/left_for_review — the period readiness
      // the CloseChecklist gates on must refetch (prefix: every period).
      queryClient.invalidateQueries({ queryKey: ["recon-close-readiness"] });
      // Same reason as the single-row approve: a bulk approve makes many results
      // terminal at once, and the resolution surfaces gate their reject control on
      // that. This is the worst version of the stale-row problem, not the mildest.
      queryClient.invalidateQueries({ queryKey: ["recon-group-proposals"] });
      queryClient.invalidateQueries({ queryKey: ["recon-needs-human-proposals"] });
      queryClient.invalidateQueries({ queryKey: ["recon-resolution-summary"] });
    },
  });
}
