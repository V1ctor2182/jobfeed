import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { ApplyHealth, useApplyHealth } from "@/api/workspace";
import { label, LoadingSnapshot, Metrics, timestamp } from "./shared";

export function ApplyReadStatus({ query }: { query: ReturnType<typeof useApplyHealth> }) {
  if (query.isPending) return <LoadingSnapshot label="Loading application health" />;
  if (query.isError) return <Alert type="warning" header={query.data ? "Apply health refresh failed" : "Apply health unavailable"}>
    {query.data ? `Showing the last successful read from ${timestamp(query.dataUpdatedAt)}. These observations may now be stale.` : "Application records could not be read. Refresh to try again."}
  </Alert>;
  return null;
}

export function QueueSummary({ data }: { data: ApplyHealth }) {
  const available = data.queue.state === "available";
  return <SpaceBetween size="m">
    <Metrics items={[{ label: "Stored queue records", value: available ? data.queue.total : null }]} />
    {available ? <SpaceBetween size="s">
      {data.queue.error && <Alert type="warning" header="Queue details are incomplete">{label(data.queue.error)}. Queue counts remain available.</Alert>}
      {(data.queue.total ?? 0) === 0 ? <Box>No application jobs are queued.</Box> : <ul className="workspace-state-counts" aria-label="Global queue states">
        {Object.entries(data.queue.counts_by_state ?? {}).map(([state, count]) => <li key={state}><strong>{count}</strong> {label(state)}</li>)}
      </ul>}
      <Box variant="small" color="text-body-secondary">Totals include all stored states, including completed records. Queue states do not establish live worker activity.</Box>
    </SpaceBetween> : <Alert type="info" header={data.queue.state === "missing" ? "Application queue is not initialized" : "Application queue unavailable"}>
      {data.queue.error ? label(data.queue.error) : "No readable queue records are available."} Counts are unknown.
    </Alert>}
    {data.runtime.state !== "available" && <Alert type="info" header="Apply runtime is not available">
      {data.runtime.error ? label(data.runtime.error) : "The combined workspace has no configured application runtime."}
    </Alert>}
  </SpaceBetween>;
}
