import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { components } from "@/api/types.gen";

export type TrackerState = components["schemas"]["TrackerState"];
export type TrackerMessage = components["schemas"]["TrackerMessage"];
export type TrackerBody = components["schemas"]["TrackerBody"];
export type TrackerApplication = components["schemas"]["TrackerApplication"];
export type Stage = TrackerApplication["stage"];
export type MailStatus = TrackerMessage["status"];

export function useTracker() {
  return useQuery({ queryKey: ["tracker"] as const, queryFn: () => apiFetch<TrackerState>("/api/tracker"), refetchInterval: 5 * 60_000, retry: false });
}

/** One email with its body. The first read fetches it from Gmail and can take several seconds; it never changes after. */
export function useTrackerMessage(id: string | null) {
  return useQuery({
    queryKey: ["tracker", "message", id] as const,
    queryFn: () => apiFetch<TrackerBody>(`/api/tracker/messages/${id}`),
    enabled: id !== null,
    staleTime: Infinity,
    retry: false,
  });
}
