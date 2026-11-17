import Alert from "@cloudscape-design/components/alert";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import type { BatchHealth } from "@/api/workspace";
import { ObservedJobDetails, ObservedJobs } from "@/components/workspace/ApplicationTable";
import { label, Metrics, RouteLink, timestamp } from "@/components/workspace/shared";
import { ApplicationJobLinks } from "./ApplicationJobLinks";
import { ApplicationFormSnapshot } from "./ApplicationFormSnapshot";
import { applicationJobState, batchStatus, observationStatus } from "./run-model";

export function ApplicationRunDetail({ batch, jobId }: { batch: BatchHealth; jobId?: number }) {
  const valid = batch.source_binding === "valid";
  const job = valid ? batch.jobs?.find(item => item.job_id === jobId) : undefined;
  return <SpaceBetween size="m">
    <StatusIndicator type={valid && batch.freshness === "fresh" ? "info" : "warning"}>{job ? `${observationStatus(batch)} · ${applicationJobState(job)}` : batchStatus(batch)}</StatusIndicator>
    <Box>Observed {timestamp(batch.checked_at)}. Saved observations do not prove a worker is currently running.</Box>
    {(batch.issues ?? []).length > 0 && <Alert type="warning" header="Observation needs review">{batch.issues!.map(label).join(" · ")}</Alert>}
    {!valid && <Alert type="warning">The observation source is {batch.source_binding}. Progress cannot be verified.</Alert>}
    {jobId !== undefined && !job && <Alert type="warning">This job is unavailable in the selected batch observation.</Alert>}
    {job && <>
      {job.applier_label && <Box variant="small">Applier {job.applier_label}</Box>}
      <ApplicationJobLinks batchId={batch.id} job={job} />
      <Box variant="small">Open application form brings the applier’s existing Chrome tab forward.</Box>
      <ApplicationFormSnapshot batchId={batch.id} jobId={job.job_id} />
      <ObservedJobDetails job={job} />
    </>}
    {valid && <ExpandableSection headerText="Batch diagnostics" defaultExpanded={jobId === undefined}>
      <Box variant="small"><span className="workspace-wrap">Batch {batch.id}</span></Box>
      <Metrics items={[
        { label: "Submitted", value: batch.counts?.submitted }, { label: "Synced", value: batch.counts?.synced },
        { label: "Tab closure verified", value: batch.counts?.closed }, { label: "Occupied", value: batch.counts?.occupied },
        { label: "Paused", value: batch.counts?.paused },
      ]} />
      <Box variant="small">Submission, sync, and browser tab closure are separate evidence counts.</Box>
      <Metrics items={[
        { label: "Pending controller", value: batch.blockers?.pending_controller },
        { label: "Awaiting user", value: batch.blockers?.awaiting_user },
        { label: "Waiting for result", value: batch.blockers?.waiting_result },
      ]} />
      {jobId === undefined && <ObservedJobs key={batch.id} batch={batch} />}
    </ExpandableSection>}
    <RouteLink href="/apply-health">Open queue diagnostics</RouteLink>
  </SpaceBetween>;
}
