import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { components } from "@/api/types.gen";
import { checklistKey } from "@/api/checklist";

export type ReadinessCheck = components["schemas"]["ReadinessCheck"];
export type ReadinessRun = components["schemas"]["ReadinessRun"];
type ReadinessSnapshot = components["schemas"]["ReadinessSnapshot"];
type Dispatch = { phase: "idle" } | { phase: "posting" | "unknown"; previousRun: string | null };
const readinessKey = ["workspace", "apply-readiness"];
const pendingPhases = new Set<ReadinessRun["phase"]>(["running", "dispatch_unknown", "cleanup_unknown"]);

export function useReadiness() {
  const client = useQueryClient();
  const [dispatch, setDispatch] = useState<Dispatch>({ phase: "idle" });
  const posting = useRef(false);
  const query = useQuery({
    queryKey: readinessKey,
    queryFn: ({ signal }) => apiFetch<ReadinessSnapshot>("/api/apply-readiness", { signal }),
    retry: false,
    refetchInterval: (current) => {
      const observed = current.state.data?.run;
      const waitingForDispatch = dispatch.phase === "posting" || (dispatch.phase === "unknown" && (!observed || observed.run_id === dispatch.previousRun));
      return waitingForDispatch || (observed && pendingPhases.has(observed.phase)) ? 2_000 : observed ? 30_000 : false;
    },
  });
  const run = query.data?.run;
  const unresolved = dispatch.phase === "unknown" && (!run || run.run_id === dispatch.previousRun);
  const busy = dispatch.phase === "posting" || unresolved || Boolean(run && pendingPhases.has(run.phase));

  async function start() {
    if (posting.current || busy || !query.data?.csrf_token || query.isError) return;
    posting.current = true;
    const previousRun = run?.run_id ?? null;
    setDispatch({ phase: "posting", previousRun });
    try {
      await client.cancelQueries({ queryKey: readinessKey });
      const result = await apiFetch<ReadinessRun>("/api/apply-readiness", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Readiness-CSRF": query.data.csrf_token },
        body: JSON.stringify({ request_id: crypto.randomUUID() }),
      });
      client.setQueryData<ReadinessSnapshot>(readinessKey, { csrf_token: query.data.csrf_token, run: result });
      void client.invalidateQueries({ queryKey: checklistKey });
      setDispatch({ phase: "idle" });
    } catch {
      setDispatch({ phase: "unknown", previousRun });
      void query.refetch();
    } finally {
      posting.current = false;
    }
  }

  return { query, run, busy, unresolved, start };
}
