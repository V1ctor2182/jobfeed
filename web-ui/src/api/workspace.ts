import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { components } from "@/api/types.gen";
import type { InsightsOverviewResponse } from "@/api/queries";

export type ApplyHealth = components["schemas"]["ApplyHealthResponse"];
export type BatchHealth = NonNullable<ApplyHealth["batches"]>[number];
export type QueueJob = NonNullable<ApplyHealth["queue"]["jobs"]>[number];
export type ObservedJob = NonNullable<BatchHealth["jobs"]>[number];
export const WORKSPACE_POLL_MS = 30_000;

export function useApplyHealth() {
  return useQuery({
    queryKey: ["workspace", "apply-health"],
    queryFn: ({ signal }) => apiFetch<ApplyHealth>("/api/apply-health", { signal }),
    refetchInterval: WORKSPACE_POLL_MS,
    retry: false,
  });
}

export function useDiscoverySummary() {
  return useQuery({
    queryKey: ["insights", "overview", "all"],
    queryFn: ({ signal }) => apiFetch<InsightsOverviewResponse>("/api/insights/overview?window=all", { signal }),
    refetchInterval: WORKSPACE_POLL_MS,
    retry: false,
  });
}
