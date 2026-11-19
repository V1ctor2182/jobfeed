import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, apiFetch } from "./client";

import type { components } from "./types.gen";

export type QueueAdmissionReceipt = components["schemas"]["ApplicationQueueReceipt"];
export type ApplicationQueueControl = components["schemas"]["ApplicationQueueState"];

type QueueAttempt = { requestId: string; realJobIds: string[]; state: "sending" | "uncertain" };
export const applicationQueueKey = ["application-queue"];
const attemptKey = ["application-queue-attempt"];
const resultKey = ["application-queue-result"];
const storageKey = "jobfeed-application-queue-attempt";

function savedAttempt(): QueueAttempt | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
    return saved && typeof saved.requestId === "string" && Array.isArray(saved.realJobIds)
      && saved.realJobIds.every((id: unknown) => typeof id === "string")
      ? { requestId: saved.requestId, realJobIds: saved.realJobIds, state: "uncertain" } : null;
  } catch { return null; }
}

export function useApplicationQueueAdmission(enabled = true) {
  const client = useQueryClient();
  const attempt = useQuery<QueueAttempt | null>({
    queryKey: attemptKey, queryFn: savedAttempt, initialData: savedAttempt, enabled: false, gcTime: Infinity,
  });
  const result = useQuery<QueueAdmissionReceipt | null>({
    queryKey: resultKey, queryFn: () => null, initialData: null, enabled: false, gcTime: Infinity,
  });
  const status = useQuery({
    queryKey: [...applicationQueueKey, attempt.data?.requestId ?? "all"],
    queryFn: ({ signal }) => apiFetch<ApplicationQueueControl>(`/api/application-queue${attempt.data
      ? `?${new URLSearchParams({ request_id: attempt.data.requestId })}` : ""}`, { signal }),
    enabled, retry: false, refetchInterval: 10_000,
  });
  const acknowledged = status.data?.requests.find(request => request.request_id === attempt.data?.requestId && request.intent === "queue");
  useEffect(() => {
    if (!acknowledged || !attempt.data || attempt.data.state !== "uncertain") return;
    client.setQueryData(resultKey, acknowledged);
    client.setQueryData(attemptKey, null);
    sessionStorage.removeItem(storageKey);
    void client.invalidateQueries({ queryKey: ["workspace", "apply-health"] });
    void client.invalidateQueries({ queryKey: ["real-jobs"] });
  }, [acknowledged, attempt.data, client]);

  const submit = async (realJobIds: string[]) => {
    if (!enabled || (realJobIds.length === 0 || realJobIds.length > 100) || !status.data?.csrf_token || status.isError
      || client.getQueryData<QueueAttempt | null>(attemptKey)) return;
    const pending: QueueAttempt = { requestId: crypto.randomUUID(), realJobIds: [...realJobIds], state: "sending" };
    sessionStorage.setItem(storageKey, JSON.stringify(pending));
    client.setQueryData(attemptKey, pending);
    client.setQueryData(resultKey, null);
    try {
      const response = await apiFetch<QueueAdmissionReceipt>("/api/application-queue", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Application-Queue-CSRF": status.data.csrf_token },
        body: JSON.stringify({ request_id: pending.requestId, real_job_ids: pending.realJobIds }),
      });
      if (response.request_id !== pending.requestId || response.intent !== "queue") throw new Error("Queue request receipt could not be matched.");
      client.setQueryData(resultKey, response);
      client.setQueryData(attemptKey, null);
      sessionStorage.removeItem(storageKey);
      void client.invalidateQueries({ queryKey: applicationQueueKey });
      void client.invalidateQueries({ queryKey: ["workspace", "apply-health"] });
      void client.invalidateQueries({ queryKey: ["real-jobs"] });
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
        client.setQueryData(attemptKey, null);
        sessionStorage.removeItem(storageKey);
      } else {
        const uncertain: QueueAttempt = { ...pending, state: "uncertain" };
        sessionStorage.setItem(storageKey, JSON.stringify(uncertain));
        client.setQueryData(attemptKey, uncertain);
      }
      void client.invalidateQueries({ queryKey: applicationQueueKey });
      throw error;
    }
  };
  return { status, attempt: acknowledged && attempt.data?.state === "uncertain" ? null : attempt.data, result: result.data, submit };
}
