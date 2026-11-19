import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, apiPost } from "@/api/client";
import type { components } from "@/api/types.gen";

export type ReviewState = components["schemas"]["ReviewState"];
export type ReviewItem = components["schemas"]["ReviewItem"];
export type ReviewSite = components["schemas"]["ReviewSite"];
export type ReviewAction = components["schemas"]["ReviewReply"]["action"];
export const reviewKey = ["review"] as const;

/** Blockers the user answers with a fact or a choice. Every other blocker only says the Applier is stuck. */
const ANSWERABLE_BLOCKERS = ["submit_unknown", "tab_missing", "missing_fact"];

/** A question has an answer the user can give. A blocked job needs the user to do something elsewhere, or to drop it. */
export function isQuestion(item: ReviewItem): boolean {
  return item.kind === "question" || ANSWERABLE_BLOCKERS.includes(item.blocker_type ?? "");
}

export function useReview() {
  return useQuery({ queryKey: reviewKey, queryFn: () => apiFetch<ReviewState>("/api/review"), refetchInterval: 30_000, retry: false });
}

/** One click sends one reply. The click ID lets the server treat a repeated request as the same click. */
export function useReviewReply() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (reply: { request_id: string; action: ReviewAction; text?: string }) =>
      apiPost<ReviewState>("/api/review/reply", { ...reply, click_id: crypto.randomUUID() }),
    onSuccess: (state) => client.setQueryData(reviewKey, state),
  });
}

export function useReviewIgnore() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (jobId: number) => apiPost<ReviewState>("/api/review/ignore", { job_id: jobId, click_id: crypto.randomUUID() }),
    onSuccess: (state) => client.setQueryData(reviewKey, state),
  });
}

/** The user says they signed in to a job board: its queued jobs may start again and its stopped Appliers retry. */
export function useResumeSite() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (site: string) => apiPost<ReviewState>("/api/review/resume-site", { site, click_id: crypto.randomUUID() }),
    onSuccess: (state) => client.setQueryData(reviewKey, state),
  });
}

/** Things the user has to act on: real questions, plus one action per job board that needs a sign-in. */
export function actionCount(state: ReviewState): number {
  return state.items.filter(isQuestion).length + (state.sites ?? []).length;
}
