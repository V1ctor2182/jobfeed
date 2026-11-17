import { apiFetch } from "./client";

export interface SubmissionPreview {
  eligible: boolean; reason: string | null; scope: string; job_id: number; owner: string;
  company: string; title: string; bank_revision: number; warnings: string[];
  answers: { entry_id: string; version: number; question: string; answer: string; state: string; scope: unknown; valid_until: string | null }[];
  [key: string]: unknown;
}
export interface SubmissionRequest { request_id: string; state: string; reason?: string | null; submitted?: boolean; synced?: boolean; accepted?: boolean; delivery_not_started?: boolean; }
export type PreviewResponse = SubmissionPreview & { csrf_token: string };
export const getSubmissionPreview = (batchId: string, jobId: number) => apiFetch<PreviewResponse>(`/api/application-submissions/preview?${new URLSearchParams({ batch_id: batchId, job_id: String(jobId) })}`);
export const getSubmissionStatus = (requestId: string) => apiFetch<SubmissionRequest>(`/api/application-submissions/status?${new URLSearchParams({ request_id: requestId })}`);
export function confirmSubmission(preview: PreviewResponse, requestId: string) {
  const { csrf_token, ...expected_preview } = preview;
  return apiFetch<SubmissionRequest>("/api/application-submissions/confirm", {
    method: "POST", headers: { "Content-Type": "application/json", "X-Application-Submissions-CSRF": csrf_token },
    body: JSON.stringify({ request_id: requestId, expected_preview, confirmed: true }),
  });
}
