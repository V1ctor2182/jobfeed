import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, apiFetch } from "./client";

export interface ApplicationRerun {
  request_id: string;
  batch_id: string;
  job_id: number;
  owner_thread_id: string;
  state: "queued" | "unknown" | "running" | "completed" | "blocked";
  reason: string | null;
  created_at: string;
  turn_id: string | null;
  phase?: "relay_pending" | "cleanup" | "create_unknown" | "candidate_received" | "adopting" | "adopted" | "filling" | "completed" | "blocked" | null;
  replacement_thread_id?: string | null;
}

export interface ApplicationRerunStatus {
  csrf_token: string;
  eligible: boolean;
  reason: string | null;
  owner_thread_id: string | null;
  request: ApplicationRerun | null;
}

type Attempt = { requestId: string; owner: string; state: "sending" | "uncertain"; createdAt: number };
export const applicationRerunKey = (batchId: string, jobId: number) => ["application-reruns", batchId, jobId];

function savedAttempt(key: string): Attempt | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) ?? "null");
    return saved && typeof saved.requestId === "string" && typeof saved.owner === "string"
      ? { requestId: saved.requestId, owner: saved.owner, state: "uncertain", createdAt: typeof saved.createdAt === "number" ? saved.createdAt : Date.now() } : null;
  } catch { return null; }
}

export function useApplicationRerun(batchId: string, jobId: number) {
  const client = useQueryClient();
  const key = applicationRerunKey(batchId, jobId);
  const attemptKey = [...key, "attempt"];
  const storageKey = JSON.stringify(attemptKey);
  const attempt = useQuery<Attempt | null>({
    queryKey: attemptKey, queryFn: () => savedAttempt(storageKey),
    initialData: () => savedAttempt(storageKey), enabled: false, gcTime: Infinity,
  });
  const status = useQuery({
    queryKey: key,
    queryFn: () => apiFetch<ApplicationRerunStatus>(`/api/application-reruns?${new URLSearchParams({ batch_id: batchId, job_id: String(jobId) })}`),
    staleTime: 5_000, refetchInterval: query => attempt.data || ["queued", "unknown", "running"].includes(query.state.data?.request?.state ?? "") ? 5_000 : 30_000, retry: false,
  });
  const setAttempt = (value: Attempt | null) => {
    client.setQueryData(attemptKey, value);
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
    } catch { return; }
  };
  const acknowledged = status.data?.request?.request_id === attempt.data?.requestId;
  useEffect(() => {
    if (!acknowledged || !attempt.data) return;
    client.setQueryData([...applicationRerunKey(batchId, jobId), "attempt"], null);
    try { sessionStorage.removeItem(storageKey); } catch { return; }
  }, [acknowledged, attempt.data, batchId, jobId, client, storageKey]);

  const canRetry = attempt.data?.state === "uncertain" && !status.isError && !status.isFetching
    && status.dataUpdatedAt >= attempt.data.createdAt && status.data?.eligible
    && !["queued", "unknown", "running"].includes(status.data.request?.state ?? "")
    && status.data.owner_thread_id === attempt.data.owner;
  const submit = async (expectedOwner: string) => {
    const current = client.getQueryData<ApplicationRerunStatus>(key);
    const previous = client.getQueryData<Attempt | null>(attemptKey);
    if ((previous && !(previous.state === "uncertain" && canRetry)) || !current?.eligible || current.owner_thread_id !== expectedOwner
      || ["queued", "unknown", "running"].includes(current.request?.state ?? "")) return;
    const pending: Attempt = { requestId: previous?.requestId ?? crypto.randomUUID(), owner: expectedOwner, state: "sending", createdAt: Date.now() };
    setAttempt(pending);
    await client.cancelQueries({ queryKey: key, exact: true });
    try {
      const result = await apiFetch<ApplicationRerunStatus>("/api/application-reruns", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Application-Reruns-CSRF": current.csrf_token },
        body: JSON.stringify({ request_id: pending.requestId, batch_id: batchId, job_id: jobId, expected_owner: expectedOwner }),
      });
      client.setQueryData(key, result);
      setAttempt(null);
    } catch (error) {
      const rejected = error instanceof ApiError && error.status >= 400 && error.status < 500;
      setAttempt(rejected ? null : { ...pending, state: "uncertain" });
      await status.refetch();
      throw error;
    }
  };
  return { status, attempt: acknowledged ? null : attempt.data, canRetry, submit };
}
