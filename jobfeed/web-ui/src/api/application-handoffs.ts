import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, apiFetch } from "./client";
import { applicationQueueKey, type ApplicationQueueControl, type QueueAdmissionReceipt } from "./application-queue";
import type { components } from "./types.gen";

type HandoffState = components["schemas"]["ApplicationHandoffState"];
export type HandoffRuntime = QueueAdmissionReceipt["runtime"];
type HandoffAttempt = { requestId: string; jobIds: number[]; runtime: HandoffRuntime; state: "sending" | "uncertain" | "acknowledged" };
const attemptKey = ["application-controller-handoff"];
const storageKey = "jobfeed-controller-handoff";

function savedAttempt(): HandoffAttempt | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
    return value && typeof value.requestId === "string" && Array.isArray(value.jobIds)
      && value.jobIds.every((id: unknown) => typeof id === "number" && Number.isSafeInteger(id) && id > 0)
      ? { requestId: value.requestId, jobIds: value.jobIds, runtime: value.runtime === "claude" ? "claude" : "codex", state: value.state === "acknowledged" ? "acknowledged" : "uncertain" } : null;
  } catch { return null; }
}

export function useControllerHandoff() {
  const client = useQueryClient();
  const attempt = useQuery<HandoffAttempt | null>({ queryKey: attemptKey, queryFn: savedAttempt, initialData: savedAttempt, enabled: false, gcTime: Infinity });
  const control = useQuery({
    queryKey: [...applicationQueueKey, "all"], queryFn: ({ signal }) => apiFetch<ApplicationQueueControl>("/api/application-queue", { signal }),
    retry: false, staleTime: 10_000,
  });
  const status = useQuery({
    queryKey: ["application-controller-handoff-status", attempt.data?.requestId],
    queryFn: ({ signal }) => apiFetch<HandoffState>(`/api/application-queue/handoff?${new URLSearchParams({ request_id: attempt.data!.requestId })}`, { signal }),
    enabled: Boolean(attempt.data), retry: false, refetchInterval: 10_000,
  });
  const request = status.data?.request?.intent === "handoff" ? status.data.request : undefined;
  const acknowledged = request?.request_id === attempt.data?.requestId && request?.intent === "handoff";
  useEffect(() => {
    if (!acknowledged || !attempt.data || attempt.data.state !== "uncertain") return;
    const saved: HandoffAttempt = { ...attempt.data, state: "acknowledged" };
    sessionStorage.setItem(storageKey, JSON.stringify(saved));
    client.setQueryData(attemptKey, saved);
    void client.invalidateQueries({ queryKey: ["workspace", "apply-health"] });
  }, [acknowledged, attempt.data, client]);

  const submit = async (jobIds: number[], runtime: HandoffRuntime = "codex") => {
    const previous = client.getQueryData<HandoffAttempt | null>(attemptKey);
    if (jobIds.length === 0 || jobIds.length > 100 || !control.data?.csrf_token || control.isError
      || previous && (previous.state !== "acknowledged" || !request || status.isError) || request?.status === "unknown_delivery") return;
    const pending: HandoffAttempt = { requestId: crypto.randomUUID(), jobIds: [...jobIds], runtime, state: "sending" };
    sessionStorage.setItem(storageKey, JSON.stringify(pending));
    client.setQueryData(attemptKey, pending);
    try {
      const receipt = await apiFetch<QueueAdmissionReceipt>("/api/application-queue/handoff", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Application-Queue-CSRF": control.data.csrf_token },
        body: JSON.stringify({ request_id: pending.requestId, selection: { kind: "source", source_job_ids: pending.jobIds }, authorize_dispatch: true,
          ...(runtime === "claude" ? { runtime } : {}) }),
      });
      if (receipt.request_id !== pending.requestId || receipt.intent !== "handoff" || receipt.runtime !== runtime) throw new Error("Controller handoff receipt could not be matched.");
      await client.cancelQueries({ queryKey: ["application-controller-handoff-status", pending.requestId], exact: true });
      client.setQueryData(["application-controller-handoff-status", pending.requestId], { request: receipt });
      const saved: HandoffAttempt = { ...pending, state: "acknowledged" };
      sessionStorage.setItem(storageKey, JSON.stringify(saved));
      client.setQueryData(attemptKey, saved);
      void client.invalidateQueries({ queryKey: applicationQueueKey });
      void client.invalidateQueries({ queryKey: ["workspace", "apply-health"] });
      return receipt;
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
        sessionStorage.removeItem(storageKey);
        client.setQueryData(attemptKey, null);
      } else {
        const uncertain: HandoffAttempt = { ...pending, state: "uncertain" };
        sessionStorage.setItem(storageKey, JSON.stringify(uncertain));
        client.setQueryData(attemptKey, uncertain);
      }
      throw error;
    }
  };
  return { control, status, request, attempt: attempt.data, submit };
}
