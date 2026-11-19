import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "./client";

export interface ApplicationTabBinding {
  job_id: number;
  owner_thread_id: string;
  binding_id: string;
  state: "connected" | "disconnected" | "tab_closed" | "page_changed";
}

export interface ApplicationTabs {
  csrf_token: string;
  bindings: ApplicationTabBinding[];
}

export type ApplicationTabFocusStatus = "focused" | "tab_closed" | "page_changed" | "focus_failed";
export const applicationTabsKey = ["application-tabs"];

export function useApplicationTabs(enabled: boolean) {
  return useQuery({
    queryKey: applicationTabsKey,
    queryFn: getApplicationTabs,
    enabled,
    staleTime: 10_000,
    refetchInterval: 10_000,
    retry: false,
  });
}

export function getApplicationTabs() {
  return apiFetch<ApplicationTabs>("/api/application-tabs");
}

export function openExistingApplicationTab(jobId: number, owner: string, csrf: string) {
  return apiFetch<{ status: ApplicationTabFocusStatus }>(`/api/application-tabs/${jobId}/open-existing`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Application-Tabs-CSRF": csrf },
    body: JSON.stringify({ owner_thread_id: owner }),
  });
}
