import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "./client";
import type { components } from "./types.gen";
import { WORKSPACE_POLL_MS } from "./workspace";

export type ApplicationForm = components["schemas"]["ApplicationFormResponse"];
export type FormSnapshot = NonNullable<ApplicationForm["snapshots"]>[number];
export type FormField = NonNullable<FormSnapshot["fields"]>[number];

export function useApplicationForm(jobId: number, batchId: string) {
  return useQuery({
    queryKey: ["workspace", "application-form", batchId, jobId],
    queryFn: ({ signal }) => apiFetch<ApplicationForm>(
      `/api/apply-health/jobs/${jobId}/form?batch_id=${encodeURIComponent(batchId)}`, { signal },
    ),
    refetchInterval: WORKSPACE_POLL_MS,
    retry: false,
  });
}
