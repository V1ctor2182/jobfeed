import { Navigate, useLocation } from "react-router";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import type { ApplyHealth } from "@/api/workspace";
import { ApplicationTable } from "@/components/workspace/ApplicationTable";
import { label } from "@/components/workspace/shared";

export default function ApplicationQueuePage() {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  params.set("tab", "queue");
  return <Navigate to={{ pathname: "/runs", search: params.toString(), hash: location.hash }} replace />;
}

export function ApplicationQueueContent({ data }: { data: ApplyHealth }) {
  return <SpaceBetween size="l">
    <Box>Select queued jobs and click Apply. A Claude Applier fills each form and stops before Submit.</Box>
    {data.queue.state !== "available" && <Alert type="info" header={data.queue.state === "missing" ? "Application queue is not initialized" : "Application queue unavailable"}>
      {data.queue.error ? label(data.queue.error) : "No readable queue records are available."}
    </Alert>}
    {data.runtime.state !== "available" && <Alert type="info" header="Apply runtime is not available">
      {data.runtime.error ? label(data.runtime.error) : "The combined workspace has no configured application runtime."}
    </Alert>}
    {data.queue.state === "available" && <ApplicationTable enableHandoff jobs={data.queue.jobs ?? []} truncated={data.queue.truncated} />}
  </SpaceBetween>;
}
