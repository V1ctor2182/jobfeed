import { useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useApplyHealth } from "@/api/workspace";
import { ApplicationTable, ObservedJobs } from "@/components/workspace/ApplicationTable";
import { ApplyReadStatus, QueueSummary } from "@/components/workspace/ApplySummary";
import { BatchObservation } from "@/components/workspace/BatchObservation";
import { ConfirmationLookup } from "@/components/workspace/ConfirmationEvidence";
import { ReadinessCheck } from "@/components/workspace/ReadinessCheck";
import { timestamp } from "@/components/workspace/shared";

export default function ApplyHealthPage() {
  const query = useApplyHealth();
  const [batchId, setBatchId] = useState<string | null>(null);
  const data = query.data;
  const batches = data?.batches ?? [];
  const batch = batches.find((item) => item.id === batchId) ?? batches[0];
  return <SpaceBetween size="l">
    <div className="workspace-toolbar">
      <div><Box>Inspect queue records and saved worker observations.</Box><div className="workspace-metadata">Read-only monitoring · Refreshes every 30 seconds · Last read {timestamp(query.dataUpdatedAt)}</div></div>
      <Button iconName="refresh" loading={query.isFetching} onClick={() => { void query.refetch(); }}>Refresh</Button>
    </div>
    <ReadinessCheck />
    <ApplyReadStatus query={query} />
    <ConfirmationLookup />
    {data && <>
      <Container header={<Header variant="h2">Global queue</Header>}><QueueSummary data={data} /></Container>
      {data.queue.state === "available" && <ApplicationTable jobs={data.queue.jobs ?? []} truncated={data.queue.truncated} />}
      <BatchObservation data={data} batch={batch} onSelect={setBatchId} />
      {batch && <ObservedJobs key={batch.id} batch={batch} />}
    </>}
  </SpaceBetween>;
}
