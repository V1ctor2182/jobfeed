import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import type { ApplyHealth, BatchHealth } from "@/api/workspace";
import { label, Metrics, timestamp } from "./shared";

const FRESHNESS = {
  fresh: "Recently observed", stale: "Observation stale", missing: "Observation missing",
  invalid: "Observation invalid", unavailable: "Observation unavailable",
};

export function BatchObservation({ data, batch, onSelect }: {
  data: ApplyHealth; batch: BatchHealth | undefined; onSelect: (id: string) => void;
}) {
  const batches = data.batches ?? [];
  return <Container header={<Header variant="h2" description="Saved evidence for one batch. Historical batches can contain the same jobs.">Batch observation</Header>}>
    <SpaceBetween size="m">
      {batches.length > 0 && <FormField label="Batch" description="Counts below belong only to this batch.">
        <Select ariaLabel="Batch" selectedOption={batch ? { value: batch.id, label: batch.id } : null}
          options={batches.map((item) => ({ value: item.id, label: item.id, description: FRESHNESS[item.freshness] }))}
          onChange={({ detail }) => onSelect(detail.selectedOption.value!)} />
      </FormField>}
      {data.monitoring.state !== "available" && <Alert type="info" header="No current monitoring evidence">
        {data.monitoring.error ? label(data.monitoring.error) : "No saved batch observations were found in this runtime."} Queue records remain separate.
      </Alert>}
      {data.monitoring.truncated && <Alert type="warning">Only a bounded selection of batch records is shown.</Alert>}
      {batch ? <SpaceBetween size="m">
        <div className="workspace-toolbar">
          <StatusIndicator type={batch.freshness === "fresh" && batch.source_binding === "valid" ? "info" : "warning"}>{FRESHNESS[batch.freshness]}</StatusIndicator>
          <Box variant="small" color="text-body-secondary">Observed {timestamp(batch.checked_at)}</Box>
        </div>
        {(batch.issues ?? []).length > 0 && <Alert type="warning" header="Observation needs review">{batch.issues!.map(label).join(" · ")}</Alert>}
        <Metrics items={[
          { label: "Submitted", value: batch.counts?.submitted },
          { label: "Synced", value: batch.counts?.synced },
          { label: "Tab closure verified", value: batch.counts?.closed },
          { label: "Occupied", value: batch.counts?.occupied },
          { label: "Paused", value: batch.counts?.paused },
        ]} />
        <Box variant="small" color="text-body-secondary">Submission, sync, and browser tab closure are separate evidence counts. Occupied reflects this saved observation.</Box>
        <Metrics items={[
          { label: "Pending controller", value: batch.blockers?.pending_controller },
          { label: "Awaiting user", value: batch.blockers?.awaiting_user },
          { label: "Waiting for result", value: batch.blockers?.waiting_result },
        ]} />
        <QueueIssue batch={batch} />
        <Box variant="small" color="text-body-secondary">Saved observations do not prove a worker is currently running.</Box>
      </SpaceBetween> : <Box color="text-body-secondary">Batch progress is unknown until a valid monitor snapshot is available.</Box>}
    </SpaceBetween>
  </Container>;
}

function QueueIssue({ batch }: { batch: BatchHealth }) {
  const issue = batch.queue_health;
  if (issue.state === "active_issue") return <Alert type="warning" header="Queue health incident">
    <SpaceBetween size="xs">
      <Box>{label(issue.error_type)}</Box>
      <Box>First seen {timestamp(issue.first_seen)}. Last seen {timestamp(issue.last_seen)}.</Box>
      <Box>Diagnostic delivery {label(issue.delivery_state)}.</Box>
    </SpaceBetween>
  </Alert>;
  return <StatusIndicator type={issue.state === "no_active_issue_recorded" ? "info" : "pending"}>
    {issue.state === "no_active_issue_recorded" ? "No active queue incident recorded" : `Queue incident evidence ${label(issue.state)}`}
  </StatusIndicator>;
}
